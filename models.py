from database import conn, cursor
from repositories.product_repository import ProductRepository
from repositories.lot_repository import LotRepository

product_repo = ProductRepository()
lot_repo = LotRepository()

class NewLotItem:
    def __init__(self, name: str, quant: int, price: float, date_valid: str):
        self.name = name.strip()
        self.quant = quant
        self.price = price
        self.date_valid = date_valid

    def save(self) -> tuple[int, int]:
        """
        Salva o produto (se não existir) e registra o novo lote.
        Retorna: (product_id, lot_id)
        """
    
        row = product_repo.get_by_name(self.name)

        if row:
            product_id = row[0]
        else:
            product_id = product_repo.create(self.name)

        lot_id = lot_repo.create(
            product_id=product_id,
            quant=self.quant,
            price=self.price,
            date_valid=self.date_valid
        )

        return product_id, lot_id