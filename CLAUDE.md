# due-consulta

Projeto de consulta de averbações com backend Python (FastAPI) e frontend HTML.

## Repositório GitHub

- URL: https://github.com/douglaslima201-cyber/due-consulta
- Visibilidade: privado
- Branch principal: `master`

## Atualização automática no GitHub

A cada vez que o Claude Code termina de responder, as alterações são automaticamente commitadas e enviadas ao GitHub via hook configurado em `.claude/settings.local.json`.

O hook executa:
1. `git add .` — adiciona todos os arquivos alterados
2. `git commit -m "Atualização automática via Claude Code"` — cria commit (somente se houver mudanças)
3. `git push` — envia para o GitHub

## Estrutura do projeto

```
due-consulta/
├── backend/
│   ├── main.py           # Servidor FastAPI principal
│   ├── requirements.txt  # Dependências Python
│   └── ...
├── frontend/
│   └── index.html        # Interface web
├── start.bat             # Script de inicialização (Windows)
├── start.sh              # Script de inicialização (Linux/Mac)
└── .gitignore            # Arquivos ignorados pelo git
```

## Arquivos ignorados pelo git

- Banco de dados (`*.db`)
- Uploads e resultados temporários (`uploads/`, `results/`)
- Imagens de diagnóstico (`*.png`)
- Arquivos de debug
- Cache Python (`__pycache__/`)
