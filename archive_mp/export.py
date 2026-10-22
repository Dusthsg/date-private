import pandas as pd


def make_ex(colunas: list, dados: list, nome_arquivo: str = "lotes.xlsx") -> None:
    """Recebe as colunas e os dados e salva em Excel."""
    df = pd.DataFrame(dados, columns=colunas)
    df.to_excel(nome_arquivo, index=False)
    print(f"\n[OK] Planilha '{nome_arquivo}' gerada com sucesso!")