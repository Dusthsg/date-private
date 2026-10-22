from database import conn, cursor, init_db
from controller import selector, processing_update

from views.menus import call_menu, advanced_menu
from views.screens import add_product, update_product
from repositories.lot_repository import LotRepository
from models import NewLotItem
repo = LotRepository()


def menu():
    print("\n-------- DateLimit --------")
    print("Opção 0 : Sair")
    print("Opção 1 : Listar Produtos")
    print("Opção 2 : Inserir Produto")
    print("Opção 3 : Atualizar Produto")
    print("Opção 4 : Deletar Produto")

    while True:
        try:
            return int(input("Insira a opção que deseja: "))
        except ValueError:
            print("Entrada inválida! Digite apenas números inteiros.")


def main():
    init_db()

    while True:
        opcao = menu()

        if opcao == 1:
            call_menu()
        elif opcao == 2:
           data = add_product()
           if data:
              product = NewLotItem(**data)
              prod_id, lot_id = product.save()
              print(f"Produto salvo com sucesso com ID: {prod_id} e Lote: {lot_id}")
           else:
              print("Operação cancelada")
                
        elif opcao == 3:
             _, data = advanced_menu()
             if data:  
                entity = selector(data)
                if entity: 
                 args, entity = update_product(entity)
                 processing_update(args, entity)
                else:
                  print("\nNenhum registro encontrado para Atualização.")
        elif opcao == 4:
          _, data = advanced_menu()
          if data:  
             entity = selector(data)
             if entity:  
                repo.delete(entity)
             else:
               print("\nNenhum registro encontrado para exclusão.")


        elif opcao == 0:
            print("Encerrando o programa...")
            cursor.close()
            conn.close()
            break
        else:
            print("Opção inválida! Escolha entre 0 e 4.")


if __name__ == "__main__":
    main()