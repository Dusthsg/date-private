import os
import unittest
import json
import sqlite3
from app.db import set_test_db, init_db, create_user, get_user_by_username, get_user_by_id, SQLITE_PATH
from app.run import app
from werkzeug.security import check_password_hash

class TestUserProfile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        set_test_db()
        init_db()

    def setUp(self):
        set_test_db()
        self.client = app.test_client()
        # Limpa usuários de teste anteriores no banco de testes
        with sqlite3.connect(str(SQLITE_PATH)) as conn:
            conn.execute("DELETE FROM users WHERE username LIKE 'test_user_%';")
            conn.commit()

        # Cria usuário exclusivo para o teste
        create_user("test_user_main", "Operador Principal", "senha123", pin="5555", role="operador")
        self.user = get_user_by_username("test_user_main")

    def test_unauthorized_access(self):
        """Rejeita acesso se não estiver autenticado."""
        res = self.client.get("/api/user/profile")
        self.assertEqual(res.status_code, 401)

        res_post = self.client.post("/api/user/profile", json={"name": "Novo"})
        self.assertEqual(res_post.status_code, 401)

    def test_get_user_profile(self):
        """Retorna os dados do próprio usuário autenticado."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]
            sess["username"] = self.user["username"]
            sess["name"] = self.user["name"]
            sess["role"] = self.user["role"]

        res = self.client.get("/api/user/profile")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["username"], "test_user_main")
        self.assertEqual(data["name"], "Operador Principal")
        self.assertEqual(data["pin"], "5555")

    def test_update_name_and_username(self):
        """Permite que o usuário altere seu nome e login (username)."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]
            sess["username"] = self.user["username"]
            sess["name"] = self.user["name"]
            sess["role"] = self.user["role"]

        payload = {
            "name": "Nome Alterado",
            "username": "test_user_renamed",
            "pin": "7777"
        }
        res = self.client.post("/api/user/profile", json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["name"], "Nome Alterado")
        self.assertEqual(data["username"], "test_user_renamed")

        # Verifica persistência no banco
        db_user = get_user_by_id(self.user["id"])
        self.assertEqual(db_user["name"], "Nome Alterado")
        self.assertEqual(db_user["username"], "test_user_renamed")
        self.assertEqual(db_user["pin"], "7777")

    def test_update_password(self):
        """Permite que o usuário altere sua própria senha."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]
            sess["username"] = self.user["username"]
            sess["name"] = self.user["name"]
            sess["role"] = self.user["role"]

        payload = {
            "name": self.user["name"],
            "username": self.user["username"],
            "password": "nova_super_senha_456"
        }
        res = self.client.post("/api/user/profile", json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])

        # Confirma que a nova senha é válida
        db_user = get_user_by_id(self.user["id"])
        self.assertTrue(check_password_hash(db_user["password_hash"], "nova_super_senha_456"))

    def test_prevent_duplicate_username(self):
        """Impede que o usuário mude para um username já usado por outro operador."""
        create_user("test_user_other", "Outro", "123", role="operador")

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]
            sess["username"] = self.user["username"]

        payload = {
            "name": "Tentativa Duplicada",
            "username": "test_user_other"
        }
        res = self.client.post("/api/user/profile", json=payload)
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertFalse(data["success"])
        self.assertIn("já está em uso", data["error"])

if __name__ == "__main__":
    unittest.main()
