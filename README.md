# 📦 DUE Consulta — Siscomex NF-e Automation

Sistema de consulta automatizada de DUEs a partir de chaves NF-e no Portal Único do Siscomex.

---

## 🚀 Início Rápido

### Pré-requisitos
- Python 3.10 ou superior
- pip (gerenciador de pacotes Python)

### 1. Instalar e Iniciar

**Linux / Mac:**
```bash
chmod +x start.sh
./start.sh
```

**Windows:**
```
Duplo clique em start.bat
```

**Manual:**
```bash
cd backend
pip install -r requirements.txt
python -m playwright install chromium
python main.py
```

### 2. Abrir a Interface
Abra o arquivo `frontend/index.html` no seu navegador.

> O backend deve estar rodando em `http://localhost:5000`

---

## 📂 Estrutura da Planilha de Entrada

| Chave NF-e                                   |
|----------------------------------------------|
| 35240312345678000195550010000012341234567890  |
| 41240398765432000110550010000056785678901234  |

**Regras:**
- Coluna com nome contendo: `chave`, `nfe`, `nf-e`, `nota`, `key`
- Ou qualquer coluna com strings de 44 dígitos
- Formatos aceitos: `.xlsx` e `.csv`
- Duplicatas são removidas automaticamente

---

## 📊 Planilha de Saída

| Campo           | Descrição                              |
|-----------------|----------------------------------------|
| Chave NF-e      | Chave de 44 dígitos                    |
| Status          | Averbada / Não Averbada / Não Encontrada / Erro |
| Número DUE      | Número da DUE vinculada (se averbada)  |
| Data DUE        | Data de averbação                      |
| Status DUE      | Status da DUE no Siscomex              |
| Observações     | Erros ou informações adicionais        |
| Consultado em   | Data/hora da consulta                  |

---

## ⚙️ Configuração

### Anti-Captcha
A chave Anti-Captcha já está configurada no código:
```python
ANTICAPTCHA_KEY = "6d73ae3890ea23b5d54c6240355586c2"
```

Para alterar, edite `backend/main.py` linha 20.

### Delay entre consultas
Por padrão, há 2 segundos entre cada consulta para não sobrecarregar o portal.
Altere em `backend/main.py` na função `processar_job_async`:
```python
await asyncio.sleep(2)  # Altere conforme necessário
```

---

## 🔒 Segurança e Privacidade

- Todos os dados ficam **localmente** no seu computador
- Banco SQLite local: `backend/consultas.db`
- Uploads em: `backend/uploads/`
- Resultados em: `backend/results/`
- Nenhum dado é enviado a servidores externos (exceto Anti-Captcha para resolver CAPTCHAs)

---

## 🐛 Solução de Problemas

**Backend não inicia:**
```bash
pip install flask flask-cors playwright pandas openpyxl aiohttp
python -m playwright install chromium
```

**CORS Error no browser:**
Certifique-se de abrir `frontend/index.html` diretamente (file://) ou sirva com:
```bash
cd frontend && python -m http.server 8080
```

**Portal mudou o layout:**
- Screenshots de debug são salvos em `backend/` quando o layout não é reconhecido
- Abra um issue com o screenshot para atualizar os seletores

**CAPTCHA não resolve:**
- Verifique saldo na conta Anti-Captcha
- O navegador ficará visível para resolução manual se a API falhar

---

## 📋 Endpoints da API

| Método | Endpoint              | Descrição                    |
|--------|----------------------|------------------------------|
| POST   | /api/upload          | Upload e validação da planilha |
| POST   | /api/iniciar/{id}    | Inicia o processamento        |
| GET    | /api/status/{id}     | Status em tempo real          |
| POST   | /api/cancelar/{id}   | Cancela o processamento       |
| GET    | /api/download/{id}   | Baixa o relatório Excel       |
| GET    | /api/jobs            | Lista histórico de jobs       |
| GET    | /api/health          | Health check                  |

---

## 🏗️ Tecnologias

- **Frontend:** HTML5 + CSS3 + JavaScript Vanilla
- **Backend:** Python + Flask + Flask-CORS
- **Automação:** Playwright (Chromium)
- **Planilhas:** pandas + openpyxl
- **Banco:** SQLite
- **Anti-Captcha:** anti-captcha.com API

---

*Desenvolvido para uso operacional interno. Não armazena dados permanentemente.*
