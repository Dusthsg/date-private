# Arquitetura do DateLimit

## 1. Objetivo

O DateLimit adotará a **Arquitetura Hexagonal (Ports and Adapters)** como modelo arquitetural de referência.

A escolha tem dois objetivos:

- reduzir o acoplamento entre as regras do sistema e tecnologias externas;
- usar o projeto como ambiente prático de aprendizado e experimentação com modularização, portas, adaptadores e inversão de dependências.

A arquitetura será adotada de forma incremental. O estado atual do código não deve ser tratado como se já estivesse completamente convertido para o modelo hexagonal.

## 2. Princípio central

O núcleo da aplicação deve depender de abstrações, enquanto detalhes externos dependem do núcleo.

~~~text
                 ADAPTERS DE ENTRADA
              CLI / Web / API / outros
                         │
                         ▼
              ┌─────────────────────┐
              │    APPLICATION      │
              │    Casos de uso     │
              └──────────┬──────────┘
                         │
                    PORTS / API
                         │
                         ▼
              ┌─────────────────────┐
              │       DOMAIN        │
              │ Regras e entidades  │
              └──────────┬──────────┘
                         │
                    PORTS / SPI
                         │
                         ▼
              ┌─────────────────────┐
              │ ADAPTERS DE SAÍDA   │
              │ SQLite / PostgreSQL │
              │ arquivos / e-mail   │
              └─────────────────────┘
~~~

O domínio e os casos de uso não devem conhecer detalhes de SQLite, PostgreSQL, terminal, HTTP, Excel ou outras tecnologias externas.

## 3. Estado atual

A estrutura atual ainda está em migração:

~~~text
main
  ↓
controller
  ↓
crud / repositories
  ↓
database
  ↓
SQLite
~~~

O diretório crud é considerado **legado durante a migração**. Ele não deve permanecer como uma segunda camada de persistência depois que suas responsabilidades forem transferidas.

Os repositories já iniciados na branch feat/refactoring fazem parte dessa transição.

## 4. Arquitetura alvo

A estrutura alvo será aproximadamente:

~~~text
datelimit/
├── domain/
│   ├── entities/
│   ├── value_objects/
│   └── services/
│
├── application/
│   ├── use_cases/
│   └── ports/
│
├── adapters/
│   ├── inbound/
│   │   ├── cli/
│   │   └── web/
│   │
│   └── outbound/
│       └── persistence/
│           ├── sqlite/
│           └── postgresql/
│
├── infrastructure/
│   ├── database/
│   ├── config/
│   └── logging/
│
└── main.py
~~~

Essa estrutura é uma direção arquitetural, não uma obrigação de criar todos esses diretórios imediatamente.

## 5. Responsabilidades

### Domain

Representa o núcleo das regras do negócio.

Pode conter:

- entidades;
- objetos de valor;
- regras de validade;
- regras de estoque;
- regras de status;
- serviços de domínio quando uma regra não pertencer naturalmente a uma entidade.

O domínio não deve depender de infraestrutura.

### Application

Coordena os casos de uso do sistema.

Exemplos:

- cadastrar produto;
- atualizar lote;
- consultar produtos;
- excluir lote;
- registrar movimentação.

A camada de aplicação pode depender das portas necessárias para executar esses casos de uso, mas não deve depender diretamente de uma implementação específica de banco.

### Ports

As portas definem os contratos através dos quais o núcleo conversa com o mundo externo.

Exemplo conceitual:

~~~python
class ProductRepository(Protocol):
    def get_by_id(self, product_id: int):
        ...

    def save(self, product):
        ...

    def delete(self, product_id: int):
        ...
~~~

A porta expressa **o que a aplicação precisa**, não como isso será implementado.

### Inbound adapters

Recebem comandos externos e os transformam em chamadas para a aplicação.

Exemplos:

- CLI;
- interface web;
- API;
- futuramente outros clientes.

A CLI não deve conter regras de negócio que deveriam pertencer ao núcleo.

### Outbound adapters

Implementam as portas usadas pelo núcleo.

Exemplos:

- SQLiteProductRepository;
- PostgreSQLProductRepository;
- exportador XLSX;
- envio de e-mail.

O adapter conhece a tecnologia externa. O núcleo não precisa conhecê-la.

### Infrastructure

Concentra configurações e mecanismos técnicos que conectam a aplicação ao ambiente.

Exemplos:

- criação de conexões;
- configuração;
- logging;
- inicialização de componentes;
- composição dos adapters.

## 6. Repository Pattern

O Repository Pattern continuará sendo utilizado, mas com uma distinção importante.

A aplicação deve depender do **contrato do repository**, enquanto a implementação fica no adapter de saída.

~~~text
Application
     │
     ▼
ProductRepository (port)
     ▲
     │ implements
     │
SQLiteProductRepository
~~~

Isso atende diretamente ao RNF02: a persistência deve ser desacoplada do restante da aplicação para permitir evolução de SQLite para PostgreSQL ou outros provedores.

O repository não deve ser responsável por regras de interface ou decisões de negócio.

## 7. Regra de dependências

A direção das dependências deve proteger o núcleo:

~~~text
Adapters ───────► Application ───────► Domain
    │                   │
    │                   └────► Ports
    │
    └──── implementam as portas
~~~

Uma implementação concreta de infraestrutura pode conhecer o contrato que implementa.

O inverso deve ser evitado.

Por exemplo, isto é indesejado:

~~~python
# Domain
import sqlite3
~~~

O domínio não deve importar SQLite para realizar uma regra de negócio.

## 8. Banco de dados

O SQLite continuará sendo o banco utilizado durante o desenvolvimento atual.

A arquitetura, entretanto, deve permitir que a persistência seja substituída posteriormente.

Objetivo:

~~~text
                 ProductRepository
                    (Port)
                   ▲       ▲
                   │       │
                   │       │
      SQLite Adapter       PostgreSQL Adapter
~~~

A troca do banco não deve exigir reescrever as regras de negócio.

O PostgreSQL é uma evolução prevista pelo RNF01. Essa migração deverá ocorrer quando os requisitos de concorrência e operação multiusuário justificarem a mudança.

## 9. SQL

SQL pertence ao adapter de persistência.

A aplicação não deve montar SQL para executar consultas diretamente.

Em vez disso:

~~~text
Use Case
   ↓
Repository Port
   ↓
SQLite/PostgreSQL Adapter
   ↓
SQL
~~~

Isso também mantém a lógica de entrada, validação e negócio fora das consultas SQL.

## 10. Tratamento de erros

Erros devem ser tratados no nível apropriado.

- Entrada inválida pertence à interface/aplicação.
- Regras inválidas pertencem ao domínio ou caso de uso.
- Falhas de persistência pertencem ao limite entre aplicação e adapter.
- SQL não deve receber responsabilidades de validação de entrada ou regras de negócio que pertencem ao código da aplicação.

O objetivo é evitar transformar consultas SQL em um lugar onde diferentes responsabilidades sejam misturadas.

## 11. Testabilidade

A arquitetura deve facilitar testes isolados.

Casos de uso devem poder ser testados utilizando implementações falsas das portas, sem depender necessariamente de um banco real.

Exemplo conceitual:

~~~text
Use Case
   ↓
FakeProductRepository
~~~

Testes de integração poderão testar separadamente:

~~~text
SQLiteProductRepository
        ↓
     SQLite
~~~

Antes de remover completamente o crud, o comportamento relevante deve ser validado por testes ou verificações equivalentes.

## 12. Migração incremental

A conversão será feita em etapas para reduzir risco e preservar o comportamento existente.

### Etapa 1 — Repositories

Criar e padronizar os repositories necessários.

### Etapa 2 — Migração dos usos

Substituir chamadas diretas ao crud por casos de uso/repositories conforme a responsabilidade for transferida.

### Etapa 3 — Ports

Separar os contratos das implementações concretas de persistência.

### Etapa 4 — Domain e Application

Mover gradualmente regras de negócio e casos de uso para seus respectivos módulos.

### Etapa 5 — Adapters

Organizar CLI e futuras interfaces externas como adapters de entrada e organizar persistência como adapters de saída.

### Etapa 6 — Remoção do legado

Somente após a migração e validação:

- remover crud;
- remover dependências antigas;
- atualizar testes e documentação.

## 13. O que não fazer

A adoção da arquitetura hexagonal não significa criar abstrações para tudo.

Evitar:

- interfaces sem necessidade real;
- dezenas de classes apenas para esconder funções simples;
- criar todos os módulos futuros antecipadamente;
- duplicar crud e repositories indefinidamente;
- colocar regras de negócio em controllers;
- colocar regras de negócio em SQL;
- criar adapters sem uma necessidade externa concreta.

A arquitetura deve crescer junto com o sistema.

## 14. Critério de sucesso

A arquitetura será considerada bem aplicada quando for possível alterar um detalhe externo sem exigir alterações desnecessárias no núcleo.

Exemplos:

- trocar SQLite por PostgreSQL;
- trocar CLI por Web;
- adicionar uma API;
- trocar o mecanismo de exportação;
- substituir uma implementação de persistência por uma implementação falsa em testes.

O objetivo não é criar a maior quantidade possível de módulos.

O objetivo é criar **fronteiras claras de responsabilidade e dependência**.

## 15. Relação com os requisitos

A arquitetura hexagonal está diretamente relacionada principalmente a:

- **RNF01** — evolução de SQLite para PostgreSQL;
- **RNF02** — Repository Pattern e desacoplamento da persistência;
- **RNF03** — integridade referencial e transações;
- **RNF04–RNF06** — possibilidade de diferentes interfaces;
- **RNF07** — execução de tarefas em background;
- **RNF09** — infraestrutura de logging e auditoria;
- **RNF10** — isolamento das operações de ML em relação às transações operacionais.

O documento de requisitos em docs/REQUIREMENTS.md continua sendo a referência para o que o sistema precisa fazer. Este documento define como a arquitetura pretende organizar essas responsabilidades.

## 16. Princípio para a evolução

> **Primeiro separar responsabilidades. Depois abstrair o que realmente precisa ser abstraído.**

O DateLimit é também um projeto de aprendizado. Portanto, decisões arquiteturais devem ser acompanhadas pela compreensão de seus motivos e não apenas pela reprodução de padrões.

A arquitetura hexagonal será usada como um modelo para experimentar modularidade, inversão de dependências, Ports and Adapters e isolamento do domínio na prática.
