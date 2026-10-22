from datetime import datetime, date, timedelta
from archive_mp.export import make_ex 
from repositories.lot_repository import LotRepository
from repositories.product_repository import ProductRepository
from views.screens import show_data

prod_repo = ProductRepository()
lot_repo = LotRepository()

def orquest(colunas: list, dados: list) -> None:
    """Recebe os dados que já foram consultados e decide a exportação."""
    if not dados:
        print("\nNenhum registro encontrado para exportar.")
        return
    show_data(colunas, dados)

    while True:
      opcao = input("\nDeseja exportar esses dados para Excel? (s/n): ").strip().lower()

      if opcao not in ["s", "n"]:
          break
    
      if opcao == "s":
        nome = input("Nome do arquivo (Enter para 'lotes.xlsx'): ").strip() or "lotes.xlsx"
        if not nome.endswith(".xlsx"):
            nome += ".xlsx"
            
        make_ex(colunas, dados)
      else:
        print("Finalizado sem exportação.")
        return
       

def query_venc():
    q_date = " AND l.date_valid < ?"
    today = date.today().strftime("%Y-%m-%d")
    dados = lot_repo.read(q_date, [today])
    colunas = lot_repo.get_columns()
    orquest(colunas, dados)


def query_days(days_min, days_max):
    today = date.today()
    init = (today + timedelta(days=days_min)).strftime("%Y-%m-%d")
    end = (today + timedelta(days=days_max)).strftime("%Y-%m-%d")

    query_search = " AND l.date_valid BETWEEN ? AND ?"
    dados = lot_repo.read(query_search, [init, end])
    colunas = lot_repo.get_columns()
    orquest(colunas, dados)


def adv_query(req_list):
    query = ""
    params = []

    if 1 in req_list:
        text = input("Insira o nome ou parte dele para buscar: ").strip()

        if not text:
             print("ERRO 301, entrada não inserida")
             return None
        
        query += " AND p.name LIKE ?"
        params.append(f"%{text}%")

    if 2 in req_list:
        input_date = input("Insira uma data específica como (20/10/2028 ou 20-10-2028): ").strip()
        if not input_date:
            print("ERRO 301, entrada não inserida")
            return None
        
        try:
            datef = datetime.strptime(input_date.replace("/", "-"), "%d-%m-%Y").strftime("%Y-%m-%d")
            query += " AND l.date_valid = ?"
            params.append(datef)
        except ValueError:
            print("Data inválida. Certifique-se de digitar no formato DD/MM/AAAA.")
            return None

    if 3 in req_list:
        datei = input("Insira um período em dias ex: (30, 45) ou apenas (30): ").strip()
        arr = [int(x.strip()) for x in datei.split(",") if x.strip().isdigit()]
        today = datetime.now().date()
        if not datei:
                    print("ERRO 301, entrada não inserida")
                    return None

        if len(arr) == 2:
            days_min, days_max = sorted(arr)
            init = today + timedelta(days=days_min)
            end = today + timedelta(days=days_max)

            query += " AND l.date_valid BETWEEN ? AND ?"
            params.append(init.strftime("%Y-%m-%d"))
            params.append(end.strftime("%Y-%m-%d"))

        elif len(arr) == 1:
            days_max = arr[0]
            end = today + timedelta(days=days_max)

            query += " AND l.date_valid BETWEEN ? AND ?"
            params.append(today.strftime("%Y-%m-%d"))
            params.append(end.strftime("%Y-%m-%d"))
        else:
            print("Entrada inválida! Digite 1 ou 2 números separados por vírgula.")
            return None

    if 4 in req_list:
        entrada_qtd = input("Insira a quantidade (ex: 10 para teto, ou 0, 10 para intervalo): ").strip()
        arr_qtd = [int(x.strip()) for x in entrada_qtd.split(",") if x.strip().isdigit()]
        if not entrada_qtd:
                    print("ERRO 301, entrada não inserida")
                    return None

        if len(arr_qtd) == 1:
            query += " AND l.quant <= ?"
            params.append(arr_qtd[0])
        elif len(arr_qtd) == 2:
            qtd_min, qtd_max = sorted(arr_qtd)
            query += " AND l.quant BETWEEN ? AND ?"
            params.append(qtd_min)
            params.append(qtd_max)
        else:
            print("Entrada de quantidade inválida! Digite 1 ou 2 números.")
            return None
    dados = lot_repo.read(query, params)
    colunas = lot_repo.get_columns()
    return colunas, dados

"""===================================================================================================================="""
"""Selector"""
"""===================================================================================================================="""

def selector(data: list):
    if not data:
        print("\nNenhum registro para selecionar.")
        return None
    
    for i, row in enumerate(data, start=1):
         lot_id, nome, prod_id, quant, price, validade = row[:6]
         print(f"[{i:<5}] Lote #{lot_id:<6} | {nome:<50} | Qtd: {quant:<5} | Preço: R$ {price:<7} | {validade:<10} ")
    print("[0] Cancelar operação")
    print("=" * 60)

    while True:
        try:
            escolha = int(input("\nEscolha o número do item desejado: "))
            
            if escolha == 0:
                print("Operação cancelada.")
                return None
            
            # Verifica se o número digitado existe na lista (entre 1 e o total de linhas)
            if 1 <= escolha <= len(data):
                # O índice real da lista em Python começa em 0, então subtrai 1:
                item_selecionado = data[escolha - 1]
                return item_selecionado
            else:
                print(f"Número fora do intervalo! Digite um valor entre 1 e {len(data)}.")
                
        except ValueError:
            print("Entrada inválida! Digite apenas números inteiros.")


def processing_update(args, entity):
    lot_id, nome, prod_id, quant, price, validade = entity[:6]

    # 1. Se escolheu alterar o Nome (Tabela 'products')
    if 1 in args:
        new_name = input(f"Digite um novo nome para [{nome}]: ").strip()
        if new_name:
            prod_repo.update_name(prod_id, {"name": new_name})
            print("-> Nome atualizado com sucesso!")

    # 2. Dicionário de campos dinâmicos para a tabela 'product_lots'
    lot_fields = {}

    if 2 in args:
        try:
            new_quant = int(input(f"Digite uma nova quantidade para [{quant}]: "))
            lot_fields["quant"] = new_quant
        except ValueError:
            print("Quantidade inválida! Campo ignorado.")

    if 3 in args:
        try:
            raw_price = input(f"Digite um novo preço para [{price:.2f}]: ").replace(",", ".")
            lot_fields["price"] = float(raw_price)
        except ValueError:
            print("Preço inválido! Campo ignorado.")

    if 4 in args:
        try:
            new_date = input("Digite a nova data em formato (22/12/2028 ou 22-12-2028): ").strip()
            datef = datetime.strptime(new_date.replace("/", "-"), "%d-%m-%Y").strftime("%Y-%m-%d")
            lot_fields["date_valid"] = datef
        except ValueError:
            print("Data inválida! Campo ignorado.")

    if 5 in args:
        try:
            new_local = input("Digite a novo local para o produto: ").strip().upper()
            lot_fields["location"] = new_local
        except ValueError:
            print("Local Inválido! Campo ignorado.")

    if 6 in args:
        try:
            new_status = input("Digite o novo status exemplo (vencido): ").strip().upper()
            lot_fields["status"] = new_status
        except ValueError:
            print("Status inválido! Campo ignorado.")

    # 3. Executa o update dinâmico via repositório
    if lot_fields:
        sucesso = lot_repo.update(lot_id, lot_fields)
        if sucesso:
            print(f"\n[OK] Lote #{lot_id} atualizado com sucesso!")
        else:
            print("\n[!] Falha ao atualizar o lote.")