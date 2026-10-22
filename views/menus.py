from controller import adv_query, query_venc, query_days, orquest
from repositories.lot_repository import LotRepository
repo = LotRepository()

def advanced_menu():
    print("\n" + "=" * 55)
    print(f"{'DATELIMIT Menu de Busca Avançada':^50}")
    print("=" * 55)
    print("[1] Nome")
    print("[2] Data Específica")
    print("[3] Período")
    print("[4] Quantidade")
    print("[0] Sair")
    print("=" * 55)
    while True:
        try:
            input_query = input("Insira o(s) número(s) referente às opções separadas por vírgula ( , ): ")
            args = [int(x.strip()) for x in input_query.split(",") if x.strip().isdigit()]

            if not args:
                print("Nenhuma opção informada")

            if (0 in args and len(args) == 1):
                return
            
            if 0 in args:
                print("Entrada Inválida! [0] é valor de sáida")
                continue

            if any(x not in (1, 2, 3, 4) for x in args):
                 print("Entrada inválida! Escolha apenas opções entre 1 e 4.")
                 continue
            
            result = adv_query(args)
            if result is None:
                continue
            return result
        
        except ValueError:
            print("Entrada inválida! Digite apenas números inteiros.")


def main_menu():
    print("\n" + "=" * 55)
    print(f"{'DATELIMIT Menu Principal':^50}")
    print("=" * 55)
    print("[1] Vencidos")
    print("[2] Crítico (7 dias)")
    print("[3] Atenção (8 - 29 dias)")
    print("[4] Alerta (30 - 45 dias)")
    print("[5] Completa (0 - 45 dias)")
    print("[6] Todos os registros")
    print("[7] Busca avançada")
    print("[0] Sair")
    print("=" * 55)

    while True:
        try:
            choose = int(input("Insira a opção que deseja: "))
            if not 0 <= choose <= 7:
               print("Opção inválida! Escolha uma opçao entre 0 e 7")
               return choose
            return choose
        except ValueError:
            print("Entrada inválida! Digite apenas números inteiros.")


def call_menu():
    while True:
        num = main_menu()

        if num == 1:
            query_venc()
        elif num == 2:
            query_days(0, 7)
        elif num == 3:
            query_days(8, 29)
        elif num == 4:
            query_days(30, 45)
        elif num == 5:
            query_days(0, 45)
        elif num == 6:
            colunas = repo.get_columns()
            dados = repo.read()
            orquest(colunas, dados)
        elif num == 7:
            column, data = advanced_menu()
            orquest(column, data)
        elif num == 0:
            print("Saindo...")
            break