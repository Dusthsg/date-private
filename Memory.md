# DateLimit — Versão Privada com Suporte a PostgreSQL

Esta versão foi configurada no repositório privado **[date-private](https://github.com/Dusthsg/date-private)** e na pasta `datelimit-private/`.

---

## 🚀 Novidades Implementadas

### 1. Suporte Nativo a PostgreSQL (com Fallback SQLite)
- **Camada Unificada (`app/db.py`)**: O sistema detecta automaticamente se a variável de ambiente `DATABASE_URL` está preenchida:
  - Se `DATABASE_URL` estiver presente: conecta no **PostgreSQL** usando `psycopg2`.
  - Se não estiver: usa o banco local **SQLite** (`database/products.db`).
- **Script de Migração Automática (`app/migrate_sqlite_to_pg.py`)**: Migra todos os produtos e lotes existentes do SQLite para o banco PostgreSQL preservando IDs, datas e relacionamentos com integridade referencial.
- **Badge do Banco no Cabeçalho**: A interface exibe em tempo real se está rodando em `PostgreSQL` ou `SQLite`.

### 2. Busca Parcial ou Integral (Exata)
- Adicionado um seletor dropdown ao lado da barra de pesquisa:
  - **🔎 Busca Parcial** (padrão): usa `ILIKE %termo%` no Postgres ou `LIKE %termo%` no SQLite. Encontra qualquer produto que contenha o texto informado.
  - **🎯 Busca Exata**: procura correspondência exata do nome do produto.

### 3. Formulário de Cadastro Rápido de Lote ("Adicionar")
- Painel colapsável no topo com visual limpo e moderno.
- Permite cadastrar:
  - **Nome do Produto** (cria produto novo ou associa ao existente)
  - **Quantidade**
  - **Preço**
  - **Data de Validade**
- Salva instantaneamente via HTMX sem recarregar a tela e atualiza a tabela e os contadores automaticamente.

---

## 🛠️ Como Usar e Rodar

### Rodando com SQLite (Padrão Local):
```bash
cd datelimit-private
pip install -r requirements.txt
python -m app.run
```

### Rodando com PostgreSQL:
Defina a variável `DATABASE_URL`:
```powershell
# No PowerShell (Windows):
$env:DATABASE_URL = "postgresql://usuario:senha@host:5432/nome_banco"
python -m app.run
```

```bash
# No Linux / Termux:
export DATABASE_URL="postgresql://usuario:senha@host:5432/nome_banco"
python -m app.run
```

### Migrar os dados do SQLite para o PostgreSQL:
```powershell
$env:DATABASE_URL = "postgresql://usuario:senha@host:5432/nome_banco"
python -m app.migrate_sqlite_to_pg
```

---

## 📱 Acesso em Rede
Assim como na versão anterior, a aplicação escuta em `0.0.0.0:5000` e exibe tanto o link `localhost:5000` quanto o IP local para acesso pelo celular conectado no mesmo Wi-Fi.
