# Acervo

Uma coleção pessoal em Streamlit para organizar, em uma única biblioteca, músicas armazenadas no Google Drive e em uma pasta local. Catálogo e favoritos são persistidos no Google Sheets — não há SQLite.

## O que já está incluído

- importação recursiva de MP3, FLAC, M4A, OGG, Opus, WAV e AAC;
- acesso a uma pasta do Google Drive compartilhada com a conta de serviço;
- coleção unificada com a origem indicada na página de cada álbum;
- leitura de título, artista, álbum, número da faixa, disco, ano e gênero;
- capas ao lado do álbum ou incorporadas no arquivo de áudio;
- navegação por álbuns, busca, favoritos, fila e player de música;
- tela inicial com estatísticas e álbuns recentes;
- página de login com usuário ou e-mail e sessão persistida por cookie;
- sincronização pelo mesmo `st.connection("gsheets")` usado no reading-dashboard;
- favoritos separados por usuário.

## Instalação no Windows

No PowerShell, dentro desta pasta:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
streamlit run app.py
```

O endereço padrão é `http://localhost:8501`.

## Conectar ao Google Sheets

1. Crie um projeto no Google Cloud e ative **Google Sheets API** e **Google Drive API**.
2. Crie uma conta de serviço e baixe a chave JSON.
3. Crie uma planilha vazia e compartilhe-a como **Editor** com o `client_email` da conta de serviço.
4. Copie o ID que fica entre `/d/` e `/edit` na URL da planilha.
5. No arquivo `.streamlit/secrets.toml`, configure a conexão.
```

O app usa `st.connection("gsheets", type=GSheetsConnection)`. As abas `musicas`, `configuracoes`, `favoritos` e `progresso` são criadas quando recebem os primeiros dados.

## Conectar a pasta de músicas do Google Drive

1. Compartilhe a pasta de músicas como **Leitor** com o mesmo `client_email` configurado em `connections.gsheets`.
2. No app, abra **Configurações**.
3. Cole a URL completa da pasta — ou somente o ID — em **Pasta de músicas do Google Drive**.
4. Clique em **Importar / atualizar Google Drive**.

As subpastas são percorridas automaticamente. O app usa acesso somente leitura e mantém um cache local dos arquivos necessários para ler metadados e reproduzir as faixas. Na primeira importação, esse processo pode demorar conforme o tamanho da coleção; as próximas importações reutilizam arquivos que não mudaram.

Para incluir também os arquivos da máquina, abra **Pasta local (opcional)** nas configurações e importe o diretório. As músicas locais e as músicas do Google Drive aparecem juntas; ao abrir um álbum, o app informa se sua origem é o Drive, a pasta local ou ambos. Quando a mesma faixa existe nas duas fontes, ela aparece apenas uma vez, usando a versão do Google Drive.

## Organizar os arquivos

O app funciona com qualquer estrutura de subpastas. Para obter o melhor resultado, mantenha os metadados dos arquivos de áudio preenchidos. Na pasta local, uma capa chamada `cover.jpg`, `folder.jpg` ou `front.jpg` na pasta do álbum será reconhecida automaticamente. No Google Drive, capas incorporadas aos arquivos de áudio são extraídas durante a importação.

## Observações

- A planilha guarda identificadores e metadados, nunca os arquivos de mídia. O cache de reprodução fica em `.acervo_cache/drive_audio` na máquina que executa o app.

