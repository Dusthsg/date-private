from database import conn as default_conn
from datetime import datetime

class LotRepository:
    def __init__(self, connection=None):
       self.conn = connection or default_conn
       self.cursor = self.conn.cursor()

    def create(self, product_id: int, quant: int, price: float, date_valid: str):
        self.cursor.execute("""
                    INSERT INTO product_lots (product_id, quant, price, date_valid) 
                    VALUES (?, ?, ?, ?)
                """, (product_id, quant, price, date_valid))
        self.conn.commit()
        lot_id = self.cursor.lastrowid
        return lot_id

    def delete(self, lot_id: int):
        if isinstance(lot_id, (list, tuple)):
            lot_id = lot_id[0]
        self.cursor.execute("DELETE FROM product_lots WHERE id = ?", [lot_id])
        self.conn.commit()
        print("produto deletado com sucesso")

    def update(self, lot_id, fields: dict) -> bool:
     if not fields:
        return False

     # Desempacota caso lot_id venha como tupla (ex: row de fetchone)
     if isinstance(lot_id, (tuple, list)):
        lot_id = lot_id[0]

    # 1. Gera: 'price = ?', 'quant = ?'
     set_clause = ", ".join([f"{col} = ?" for col in fields.keys()])
     sql = f"UPDATE product_lots SET {set_clause} WHERE id = ?"

     # 2. Extrai APENAS os valores do dict e coloca o lot_id no final
     params = list(fields.values()) + [lot_id]

    # 3. Executa passando os valores primitivos
     self.cursor.execute(sql, tuple(params))
     self.conn.commit()

     return self.cursor.rowcount > 0

    def read(self, query_search="", params=None):
        if params is None:
           params = []

        query_base = """
     SELECT 
        l.id AS lot_id,
        p.name AS product_name,
        p.id AS product_id,
        l.quant,
        l.price,
        l.date_valid,
        l.location,
        l.status
    FROM product_lots l
    INNER JOIN products p ON p.id = l.product_id
    WHERE 1=1
    """
        query_final = query_base + query_search + " ORDER BY l.date_valid ASC, p.name ASC;"
        self.cursor.execute(query_final, params)
        return self.cursor.fetchall()

    def get_columns(self):
        """Retorna os nomes das colunas da última consulta executada."""
        if self.cursor.description:
            return [desc[0] for desc in self.cursor.description]
        return []