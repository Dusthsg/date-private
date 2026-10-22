from datetime import datetime

def show_data(columns, data):
    
    print("\n" + "=" * 105)
    print(f"{'Lote':<6} | {'Produto':<50} | {'Prod ID':<8} | {'Qtd':<6} | {'Preço':<8} | {'Validade':<10}")
    print("-" * 100)
    for lin in data:
            lot_id, nome, prod_id, quant, price, validade = lin[:6]
            print(f"{lot_id:<6} | {nome:<50} | {prod_id:<8} | {quant:<6} | R${price:<6.2f} | {validade:<10}")
    print("=" * 105)

def delete(entity):
     lot_id, nome, prod_id, quant, price, validade = entity[:6]
     print(f"\nEntidade Selecionada: Lote #{lot_id} | {nome:<30} | Qtd: {quant:<5} | Preço: R$ {price:<7} | Validade: {validade}")
     print("[1] Deletar apenas este lote")
     print("[2] Deletar o produto e todos os seus lotes")
     print("[0] Cancelar")

     num = input("Digite [1] para deletar o lote e [2] para deletar o produto: ")

     if num == "1":
         aviso = "Tem certeza que deseja deletar o item selecionado s/n: ".lower().strip()
         alvo = (f"lote: ", lot_id)
         
     elif num == "2":
         print(f"\nATENÇÃO! isso deletará o '{nome}' e todos os seus lotes correpondentes do banco de dados")
         aviso = "confirma a exclusão completa do produto? (s/n):".lower().strip()
         alvo = (f"produto: ", prod_id)
     elif num == "0":
        print("Operação cancelada.")
        return None, None
     else:
        print("Opção inválida!")
        return None, None

     confirmation = input(aviso).strip().lower()
     if confirmation == "s":
         return alvo

     print("Operação cancelada.")
     return None, None

def add_product():
    print("\n--- Novo Cadastro de Lote ---")
    
    # 1. Nome
    while True:
        name = input("Nome do Produto (ou 'c' para cancelar): ").strip()
        if name.lower() == "c":
            return None
        if name:
            break
        print("-> Erro: O nome do produto não pode ficar vazio.")

    # 2. Quantidade
    while True:
        entrada = input("Quantidade: ").strip()
        try:
            quant = int(entrada)
            if quant >= 0:
                break
            print("-> Erro: A quantidade não pode ser negativa.")
        except ValueError:
            print("-> Erro: Digite um número inteiro válido.")

    # 3. Preço
    while True:
        entrada = input("Preço unitário: ").strip().replace(",", ".")
        try:
            price = float(entrada)
            if price > 0:  # Checagem corrigida para 'price'
                break
            print("-> Erro: O preço deve ser maior que 0.")
        except ValueError:
            print("-> Erro: Digite um valor numérico válido.")

    # 4. Validade
    while True:
        input_date = input("Validade (DD/MM/AAAA ou DD-MM-AAAA): ").strip()
        try:
            date_obj = datetime.strptime(input_date.replace("/", "-"), "%d-%m-%Y")
            date_valid = date_obj.strftime("%Y-%m-%d")
            break
        except ValueError:
            print("-> Erro: Data inválida! Use o formato DD/MM/AAAA.")

    return {
        "name": name,
        "quant": quant,
        "price": price,
        "date_valid": date_valid
    }

def update_product(entity):
    lot_id, nome, prod_id, quant, price, validade = entity[:6]
    print(f"\nEntidade Selecionada: Lote #{lot_id:<7} | {nome:<50} | Qtd: {quant:<4} | Preço: R$ {price:<7} | Val: {validade}")

    print("\n" + "=" * 55)
    print(f"{'Selecione o que deseja editar':^50}")
    print("=" * 55)
    print("[1] Nome do Produto")
    print("[2] Quantidade")
    print("[3] Preço")
    print("[4] Data de Validade")
    print("[5] Localidade")
    print("[6] Status")
    print("[0] Sair / Cancelar")
    print("=" * 55)

    while True:
        try:
            input_query = input("Insira as opções separadas por vírgula (ex: 2, 3): ")
            args = [int(x.strip()) for x in input_query.split(",") if x.strip().isdigit()]

            if 0 in args or not args:
                print("Operação cancelada.")
                return
            return args, entity

        except ValueError:
            print("Entrada inválida! Digite apenas números inteiros.")