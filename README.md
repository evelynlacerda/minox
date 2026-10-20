# Minox

Bot de Discord, de código aberto e escrito em Python, que fica 24h numa sala de voz, mudo e ensurdecido, em quantos servidores você quiser. Em cada servidor, um administrador escolhe a sala. Se o bot for desconectado, movido ou a conexão cair, ele volta sozinho.

- Sem tocar áudio: só presença na sala. Única intent privilegiada: *Message Content*, para os cumprimentos.
- Sem banco de dados: o estado fica em dois arquivos JSON.
- Um único arquivo de código (`bot.py`), fácil de ler e de modificar.

## Comandos

| Comando | Quem pode | O que faz |
|---|---|---|
| `/definir-sala sala:` | Administradores | Define a sala e entra nela |
| `/definir-avisos canal:` | Administradores | Escolhe onde avisar as desconexões. Sem canal, usa o chat da própria sala |
| `/sair` | Administradores | Sai da sala e desativa a permanência |
| `/status` | Todos | Mostra a sala definida, se está conectado e onde os avisos são enviados |
| `/tempo-online` | Todos | Mostra status, tempo da conexão atual e a última conexão (início, fim, duração e motivo) |

## Como funciona

- **Uma sala por servidor**, salva em `config.json`. Sobrevive a reinícios. Se o bot for removido do servidor ou a sala for apagada, a configuração daquele servidor é limpa sozinha.
- **Reconexão automática**: volta em ~5 s se for tirado da sala e confere a cada 60 s se está no lugar certo.
- **Aviso a cada desconexão**: sempre que uma conexão termina — inclusive quando o bot vai reconectar sozinho — ele manda um embed com tempo conectado, início, fim e motivo. Por padrão vai para o chat embutido da sala de voz; `/definir-avisos` aponta outro canal. Se o bot cair de forma brusca, o aviso sai quando ele volta, com o fim estimado pelo último sinal de vida.
- **Registro de conexões** em `conexoes.json`: guarda só a conexão atual e a última encerrada de cada servidor. Cada nova queda substitui a anterior. Em queda brusca (travou, máquina desligou), o fim registrado é o último sinal de vida, gravado a cada minuto.
- **Sharding automático** (`AutoShardedClient`) a partir de ~2.500 servidores.
- **Comandos globais**, registrados uma única vez. Se algum servidor mostrar comandos duplicados, coloque o ID dele em `DEV_GUILD_ID` e o bot apaga as cópias extras ao iniciar.

## Cumprimentos automáticos

Em qualquer sala de texto ou chat de voz, se a mensagem tiver "bom dia", "boa tarde" ou "boa noite" (qualquer caixa, com a última letra repetida, ex.: `BOM DIAaaa`), o bot responde conforme o horário de Brasília:

| Horário | Cumprimento aceito | Resposta |
|---|---|---|
| 06:00–11:59 | bom dia | Bom dia! |
| 12:00–17:59 | boa tarde | Boa tarde! |
| 18:00–00:00 | boa noite | Boa noite! |
| 00:01–05:59 | boa noite | Hora do meu descanso, boa noite! |

Cumprimentos fora do horário são ignorados.

## Requisitos

- Python 3.10 ou superior
- Um aplicativo de bot no [Discord Developer Portal](https://discord.com/developers/applications)
- [discord.py](https://github.com/Rapptz/discord.py) 2.7+ com suporte à criptografia de voz DAVE (instalado pelo `requirements.txt`)

## Começando

### 1. Crie o bot no Discord

1. No Developer Portal, clique em **New Application** e dê um nome (ex.: `Minox`).
2. Em **Bot → Reset Token**, copie o token. Nunca o publique.
3. Em **Bot → Privileged Gateway Intents**, ligue só **Message Content Intent** (os cumprimentos precisam ler o texto; acima de 100 servidores o Discord exige verificação). Deixe as outras desligadas e **Requires OAuth2 Code Grant** desligado.
4. Em **OAuth2 → URL Generator**, marque os scopes `bot` e `applications.commands` e as permissões `View Channels`, `Connect`, `Send Messages` e `Embed Links` (para os avisos de desconexão e as respostas de cumprimento). A URL gerada é o link de convite.

### 2. Rode localmente

```bash
git clone https://github.com/<seu-usuario>/minox.git
cd minox

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env               # Windows: copy .env.example .env
# edite o .env e cole o token em DISCORD_TOKEN

python bot.py                      # Ctrl+C para parar
```

Para desenvolver, use um segundo aplicativo (ex.: `minox-teste`), com token próprio, num servidor só seu.

### 3. Configure no Discord

Com o bot no servidor, um administrador usa `/definir-sala` e escolhe a sala de voz. O bot precisa de **Ver canal** e **Conectar** nessa sala.

Os avisos de desconexão vão para o chat embutido da sala de voz, que precisa de **Enviar mensagens** e **Inserir links**. Para usar outro canal, use `/definir-avisos canal:`; para voltar ao padrão, use `/definir-avisos` sem o canal.

## Configuração

Variáveis do arquivo `.env`:

| Variável | Obrigatória | Descrição |
|---|---|---|
| `DISCORD_TOKEN` | Sim | Token do bot |
| `DEV_GUILD_ID` | Não | ID de um servidor onde os comandos apareceram duplicados; o bot remove as cópias extras ao iniciar |

## Hospedagem 24h (Linux com systemd)

O repositório inclui `minox.service`, que mantém o bot rodando e o religa se travar. Exemplo para Ubuntu/Debian:

```bash
sudo apt update && sudo apt install -y python3-venv
git clone https://github.com/<seu-usuario>/minox.git ~/minox
cd ~/minox
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
chmod 600 .env
```

Confira em `minox.service` o `User` e os caminhos (`WorkingDirectory` e `ExecStart`); eles assumem o usuário `ubuntu` e a pasta `/home/ubuntu/minox`. Depois:

```bash
sudo cp minox.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now minox
sudo systemctl status minox        # deve mostrar active (running)
```

Em máquinas com pouca memória (1 GB), crie swap antes do `pip install`:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### Operação

```bash
sudo systemctl status minox       # está rodando?
journalctl -u minox -f            # logs ao vivo (Ctrl+C sai)
journalctl -u minox -n 100        # últimas 100 linhas
sudo systemctl restart minox      # reiniciar
sudo systemctl stop minox         # desligar
```

### Atualizar

```bash
cd ~/minox
git pull
.venv/bin/pip install -U -r requirements.txt
sudo systemctl restart minox
```

## Estrutura do projeto

| Arquivo | Para que serve |
|---|---|
| `bot.py` | O bot |
| `requirements.txt` | Dependências |
| `.env.example` | Modelo do `.env`, onde fica o token |
| `minox.service` | Serviço do systemd |
| `config.json` / `conexoes.json` | Criados pelo bot em tempo de execução. Não versione nem copie entre máquinas |

## Problemas comuns

| Sintoma | Solução |
|---|---|
| Comandos `/` não aparecem | Espere alguns minutos e use Ctrl+R no Discord. Confira se o convite incluiu `applications.commands`. |
| Comandos duplicados | Coloque o ID do servidor em `DEV_GUILD_ID` no `.env` e reinicie. |
| Integração aparece mas "não possui um bot" | O convite foi gerado sem o scope `bot`. Gere de novo com `bot` marcado. |
| "Não tenho permissão para entrar" | Na sala: Editar canal → Permissões → dê ao bot **Ver canal** e **Conectar**. |
| Os avisos de desconexão não aparecem | O bot precisa de **Enviar mensagens** e **Inserir links** no canal dos avisos (por padrão, o chat da sala de voz). Veja em `/status` qual canal está em uso. |
| Bot entra e sai em loop / `davey is not installed` | `pip install -U -r requirements.txt` e reinicie. |
| `Killed` durante o `pip install` | Falta memória: crie o swap (seção de hospedagem). |

## Contribuindo

Contribuições são bem-vindas!

1. Faça um fork e crie uma branch (`git checkout -b minha-melhoria`).
2. Faça suas alterações e verifique a sintaxe com `python -m py_compile bot.py`.
3. Teste com um bot de desenvolvimento num servidor seu.
4. Abra um pull request descrevendo o que mudou e por quê.

Encontrou um bug ou tem uma ideia? Abra uma issue com os passos para reproduzir e os logs (sem o token).

## Segurança

Nunca publique o `.env` ou o token do bot. Se o token vazar, gere outro em **Bot → Reset Token** no Developer Portal. Para reportar uma vulnerabilidade, não abra uma issue pública: entre em contato diretamente com quem mantém o projeto.
