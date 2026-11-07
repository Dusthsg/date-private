"""
Bateria de Testes Automatizados - Sistema de Login, Permissões e Auditoria Completa
DateLimit — Branch: feat/auth-audit
"""

import unittest
import os
import json
import uuid
import sqlite3
from app.run import app
from app.db import (
    SQLITE_PATH,
    get_user_by_username,
    get_user_by_pin,
    list_users,
    create_user,
    delete_user,
    log_audit,
    query_audit_logs
)


class TestAuthAndAudit(unittest.TestCase):

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_01_default_admin_exists(self):
        """Verifica se o usuário default admin foi criado com sucesso."""
        admin = get_user_by_username("admin")
        self.assertIsNotNone(admin)
        self.assertEqual(admin["username"], "admin")
        self.assertEqual(admin["role"], "admin")
        self.assertEqual(admin["pin"], "1234")

    def test_02_login_with_valid_and_invalid_credentials(self):
        """Testa o login com senha correta e incorreta."""
        # Senha incorreta
        res_fail = self.client.post("/login", data={
            "login_type": "password",
            "username": "admin",
            "password": "wrongpassword"
        })
        self.assertEqual(res_fail.status_code, 200)
        self.assertIn("incorretos", res_fail.data.decode("utf-8").lower())

        # Senha correta
        res_ok = self.client.post("/login", data={
            "login_type": "password",
            "username": "admin",
            "password": "admin123"
        }, follow_redirects=False)
        self.assertEqual(res_ok.status_code, 302)
        self.assertEqual(res_ok.headers.get("Location"), "/")

        # Verifica sessão
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("username"), "admin")
            self.assertEqual(sess.get("role"), "admin")
            self.assertEqual(sess.get("active_operator"), sess.get("name"))

    def test_03_login_with_pin(self):
        """Testa o login rápido por PIN no terminal."""
        res = self.client.post("/login", data={
            "login_type": "pin",
            "pin": "1234"
        }, follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("username"), "admin")

    def test_04_quick_operator_switch(self):
        """Testa a troca rápida de operador de chão de loja via PIN sem deslogar."""
        unique_pin = "8899"
        unique_user = f"joao_{uuid.uuid4().hex[:6]}"
        create_user(
            username=unique_user,
            name="João do Estoque",
            password="123",
            pin=unique_pin,
            role="operador"
        )

        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "admin"
            sess["name"] = "Administrador"
            sess["role"] = "admin"
            sess["active_operator"] = "Administrador"

        # Troca para João via PIN
        res = self.client.post("/api/operator/switch", json={"pin": unique_pin})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["active_operator"], "João do Estoque")

        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("active_operator"), "João do Estoque")

    def test_05_non_admin_blocked_from_admin_apis(self):
        """Garante que usuários normais (operadores) não acessam auditoria nem gestão de usuários."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = 2
            sess["username"] = "operador"
            sess["name"] = "Operador Loja"
            sess["role"] = "operador"

        # Tenta listar usuários
        res_users = self.client.get("/api/users")
        self.assertEqual(res_users.status_code, 403)

        # Tenta acessar auditoria
        res_audit = self.client.get("/api/audit/logs")
        self.assertEqual(res_audit.status_code, 403)

    def test_06_admin_can_manage_users(self):
        """Testa criação, listagem, edição e exclusão de usuários pelo Administrador."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "admin"
            sess["name"] = "Administrador"
            sess["role"] = "admin"

        unique_user = f"maria_{uuid.uuid4().hex[:6]}"
        unique_pin = str(uuid.uuid4().int)[:4]

        # Cria novo usuário
        res_create = self.client.post("/api/users", json={
            "username": unique_user,
            "name": "Maria Repositora",
            "password": "senha",
            "role": "operador",
            "pin": unique_pin
        })
        self.assertEqual(res_create.status_code, 200)
        new_u = res_create.get_json()["user"]
        self.assertIsNotNone(new_u)
        u_id = new_u["id"]

        # Edita usuário
        res_edit = self.client.post(f"/api/users/{u_id}/update", json={
            "name": "Maria Supervisora",
            "role": "admin"
        })
        self.assertEqual(res_edit.status_code, 200)

        # Deleta usuário
        res_del = self.client.post(f"/api/users/{u_id}/delete")
        self.assertEqual(res_del.status_code, 200)

    def test_07_audit_log_tracking_on_actions(self):
        """Verifica se as ações no sistema gravam registros detalhados na auditoria."""
        log_audit(
            username="Carlos Auditor",
            action="TESTE_AUDITORIA",
            details="Verificação automatizada de log",
            product_name="PRODUTO TESTE",
            ip_address="127.0.0.1"
        )

        total, logs = query_audit_logs(action="TESTE_AUDITORIA")
        self.assertGreaterEqual(total, 1)
        self.assertEqual(logs[0]["username"], "Carlos Auditor")
        self.assertEqual(logs[0]["action"], "TESTE_AUDITORIA")

    def test_08_audit_export_csv(self):
        """Testa o endpoint de exportação de logs de auditoria em formato CSV com UTF-8 BOM."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "admin"
            sess["name"] = "Administrador"
            sess["role"] = "admin"

        res = self.client.get("/api/audit/export")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/csv", res.headers.get("Content-Type"))
        content = res.data.decode("utf-8")
        self.assertIn("Quem Mexeu", content)
        self.assertIn("Ação", content)
        self.assertIn("Detalhes", content)



    def test_09_admin_panel_page_access(self):
        """Valida que apenas administradores acessam a página /admin dedicada."""
        # Não autenticado -> redireciona para login
        res_anon = self.client.get("/admin", follow_redirects=False)
        self.assertEqual(res_anon.status_code, 302)
        self.assertIn("/login", res_anon.headers.get("Location"))

        # Operador comum -> redireciona para estoque principal
        with self.client.session_transaction() as sess:
            sess["user_id"] = 2
            sess["username"] = "operador"
            sess["name"] = "Operador Loja"
            sess["role"] = "operador"

        res_op = self.client.get("/admin", follow_redirects=False)
        self.assertEqual(res_op.status_code, 302)
        self.assertEqual(res_op.headers.get("Location"), "/")

        # Administrador -> acesso autorizado 200
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "admin"
            sess["name"] = "Administrador"
            sess["role"] = "admin"

        res_admin = self.client.get("/admin")
        self.assertEqual(res_admin.status_code, 200)
        html = res_admin.data.decode("utf-8")
        self.assertIn("Painel de Administração", html)
        self.assertIn("Meu Perfil de Administrador", html)
        self.assertIn("Token de Acesso", html)

    def test_10_admin_profile_update_and_token_generation(self):
        """Valida que o administrador pode atualizar seu nome, login, pin, senha e token de API."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "admin"
            sess["name"] = "Administrador"
            sess["role"] = "admin"

        # 1. Gera novo token
        res_tok = self.client.post("/api/admin/generate-token")
        self.assertEqual(res_tok.status_code, 200)
        tok_data = res_tok.get_json()
        self.assertTrue(tok_data["success"])
        self.assertTrue(tok_data["token"].startswith("dkl_live_"))
        new_token = tok_data["token"]

        # 2. Atualiza perfil
        res_profile = self.client.post("/api/admin/profile", json={
            "name": "Admin Gestor Master",
            "username": "admin",
            "pin": "9988",
            "api_token": new_token
        })
        self.assertEqual(res_profile.status_code, 200)
        p_data = res_profile.get_json()
        self.assertTrue(p_data["success"])

        # Verifica persistência no banco
        admin_db = get_user_by_username("admin")
        self.assertEqual(admin_db["name"], "Admin Gestor Master")
        self.assertEqual(admin_db["pin"], "9988")
        self.assertEqual(admin_db["api_token"], new_token)

        # Verifica se a sessão ativa foi atualizada
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("name"), "Admin Gestor Master")


if __name__ == "__main__":
    unittest.main()
