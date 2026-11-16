import os
os.environ["DATELIMIT_TESTING"] = "1"
os.environ["TESTING"] = "true"

import unittest
from app.run import app
from app.db import set_test_db, get_field_options, add_field_option, delete_field_option

class TestAdminFieldOptions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        set_test_db()

    def setUp(self):
        self.app = app
        self.client = app.test_client()
        self.app.config["TESTING"] = True
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "admin"
            sess["role"] = "admin"

    def test_field_options_lifecycle(self):
        # 1. Verifica opções padrão
        locs = get_field_options("location")
        loc_names = [l["name"] for l in locs]
        self.assertIn("LOJA", loc_names)

        # 2. Testa endpoint GET
        res = self.client.get("/api/admin/field-options")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("success"))
        self.assertTrue(len(data.get("locations", [])) >= 2)
        self.assertTrue(len(data.get("statuses", [])) >= 3)

        # 3. Adiciona nova localidade via POST
        res_add = self.client.post(
            "/api/admin/field-options",
            json={"field_type": "location", "name": "DEPÓSITO 2"}
        )
        self.assertEqual(res_add.status_code, 200)
        locs_after = [l["name"] for l in get_field_options("location")]
        self.assertIn("DEPÓSITO 2", locs_after)

        # 4. Adiciona novo status via POST
        res_add_st = self.client.post(
            "/api/admin/field-options",
            json={"field_type": "status", "name": "LIQUIDAÇÃO"}
        )
        self.assertEqual(res_add_st.status_code, 200)
        stats_after = [s["name"] for s in get_field_options("status")]
        self.assertIn("LIQUIDAÇÃO", stats_after)

        # 5. Tenta remover opção padrão protegida LOJA -> deve falhar
        res_del_prot = self.client.delete("/api/admin/field-options/location/LOJA")
        self.assertEqual(res_del_prot.status_code, 400)

        # 6. Remove a opção criada DEPÓSITO 2
        res_del = self.client.delete("/api/admin/field-options/location/DEP%C3%93SITO%202")
        self.assertEqual(res_del.status_code, 200)
        locs_final = [l["name"] for l in get_field_options("location")]
        self.assertNotIn("DEPÓSITO 2", locs_final)

if __name__ == "__main__":
    unittest.main()
