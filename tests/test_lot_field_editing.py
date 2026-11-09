import unittest
from app.run import app
from app.db import create_lot, get_lot_by_id, delete_lot, update_lot_full

class TestLotFieldEditing(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.client = app.test_client()
        self.app.config["TESTING"] = True
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "admin"
            sess["role"] = "admin"

    def test_edit_each_field_with_date_valid_fixed(self):
        # 1. Cria um lote inicial
        _, lot_id = create_lot("BISCOITO OREO 90G", 30, 4.50, "2026-12-25", "LOJA", "NORMAL")
        self.assertIsNotNone(lot_id)

        try:
            # 2. Testa edição inline de Nome
            res_name = self.client.post(f"/lote/{lot_id}/nome", data={"name": "BISCOITO OREO RECHEADO 90G"})
            self.assertEqual(res_name.status_code, 200)
            self.assertIn("BISCOITO OREO RECHEADO 90G", res_name.get_data(as_text=True))
            lot = get_lot_by_id(lot_id)
            self.assertEqual(lot["product_name"], "BISCOITO OREO RECHEADO 90G")
            self.assertEqual(lot["date_valid"], "2026-12-25")

            # 3. Testa edição inline de Quantidade
            res_quant = self.client.post(f"/lote/{lot_id}/quantidade", data={"quant": "45"})
            self.assertEqual(res_quant.status_code, 200)
            lot = get_lot_by_id(lot_id)
            self.assertEqual(lot["quant"], 45)
            self.assertEqual(lot["date_valid"], "2026-12-25")

            # 4. Testa edição inline de Preço
            res_price = self.client.post(f"/lote/{lot_id}/preco", data={"price": "5.25"})
            self.assertEqual(res_price.status_code, 200)
            lot = get_lot_by_id(lot_id)
            self.assertAlmostEqual(lot["price"], 5.25, places=2)
            self.assertEqual(lot["date_valid"], "2026-12-25")

            # 5. Testa edição inline de Localização (LOJA -> GALPÃO)
            res_loc = self.client.post(f"/lote/{lot_id}/localizacao", data={"location": "GALPÃO"})
            self.assertEqual(res_loc.status_code, 200)
            lot = get_lot_by_id(lot_id)
            self.assertEqual(lot["location"], "GALPÃO")
            self.assertEqual(lot["date_valid"], "2026-12-25")

            # 6. Testa edição inline de Status (NORMAL -> QUEIMA-DE-ESTOQUE)
            res_stat = self.client.post(f"/lote/{lot_id}/status", data={"status": "QUEIMA-DE-ESTOQUE"})
            self.assertEqual(res_stat.status_code, 200)
            lot = get_lot_by_id(lot_id)
            self.assertEqual(lot["status"], "QUEIMA-DE-ESTOQUE")
            self.assertEqual(lot["date_valid"], "2026-12-25")

            # 7. Testa edição completa do lote via modal / endpoint /editar
            # Note que date_valid fornecida no request DEVE SER IGNORADA
            res_edit = self.client.post(f"/lote/{lot_id}/editar", data={
                "name": "BISCOITO OREO GOLDEN 90G",
                "quant": "100",
                "price": "6.00",
                "location": "LOJA",
                "status": "PROMOCIONAL",
                "date_valid": "2099-01-01"  # Tentativa de alterar data deve ser ignorada!
            })
            self.assertEqual(res_edit.status_code, 200)
            final_lot = get_lot_by_id(lot_id)
            self.assertEqual(final_lot["product_name"], "BISCOITO OREO GOLDEN 90G")
            self.assertEqual(final_lot["quant"], 100)
            self.assertAlmostEqual(final_lot["price"], 6.00, places=2)
            self.assertEqual(final_lot["location"], "LOJA")
            self.assertEqual(final_lot["status"], "PROMOCIONAL")
            # A data de validade DEVE PERMANECER ESTRITAMENTE FIXA:
            self.assertEqual(final_lot["date_valid"], "2026-12-25", "A data de validade deve ser fixa e inalterável!")

        finally:
            delete_lot(lot_id)

if __name__ == "__main__":
    unittest.main()
