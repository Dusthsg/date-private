from database import conn as default_conn

class ProductRepository:
   def __init__(self, connection=None):
      self.conn = connection or default_conn
      self.cursor = self.conn.cursor()

   def get_by_name(self, name: str):
      self.cursor.execute("SELECT id FROM products WHERE name = ?", (name,))
      return self.cursor.fetchone()
   
   def search_by_name(self, name: str) -> int:
      self.cursor.execute("SELECT id FROM products WHERE name LIKE ?", (name,))
      return self.cursor.fetchone()

   def delete(self, product_id) -> None:
      self.cursor.execute("DELETE FROM products WHERE id = ?", (product_id,))
      self.conn.commit()

   def update_name(self, new_name: str, prod_id: int) -> None:
      self.cursor.execute("UPDATE products SET name = ? WHERE id = ?", (new_name, prod_id))
      self.conn.commit()

   def create(self, name: str) -> int:
      self.cursor.execute("INSERT INTO products (name) VALUES (?)", (name,))
      self.conn.commit()

      return self.cursor.lastrowid