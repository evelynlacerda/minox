"""
Minox — fica 24h numa sala de voz do Discord, em vários servidores.

- Cada servidor tem UMA sala, escolhida por um administrador com /definir-sala.
- Entra mudo e ensurdecido, sem tocar áudio.
- Se for desconectado, movido ou a conexão cair, volta sozinho.
- A cada desconexão avisa no chat da sala (ou no canal de /definir-avisos),
  com tempo conectado, início, fim e motivo.
- /tempo-online mostra a conexão atual e a última (início, fim, duração e motivo).
- Responde "Bom dia!", "Boa tarde!" e "Boa noite!" em qualquer sala, conforme o horário de Brasília.
- Usa AutoShardedClient: escala sozinho quando passar de ~2.500 servidores.
"""

import asyncio
import json
import logging
import os
import re
import signal
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
# Opcional: servidor onde comandos ficaram duplicados; o bot limpa as cópias extras dele.
DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID") or 0)
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
SESSIONS_FILE = BASE_DIR / "conexoes.json"
RECONNECT_DELAY = 5          # segundos antes de tentar voltar
CHECK_INTERVAL = 60          # verificação periódica (segundos)
STAGGER = 0.5                # pausa entre servidores ao reconectar vários
FUSO = ZoneInfo("America/Sao_Paulo")  # horário usado nas respostas de cumprimento
AVISO_TIMEOUT = 10           # tempo máximo (segundos) para avisar as desconexões ao desligar

# Motivos que aparecem no /tempo-online
MOTIVO_SAIR = "removido com /sair"
MOTIVO_MOVIDO = "movido para outra sala"
MOTIVO_DESCONECTADO = "desconectado da sala (por alguém ou pelo Discord)"
MOTIVO_QUEDA_VOZ = "a conexão de voz caiu"
MOTIVO_SALA_APAGADA = "a sala foi apagada"
MOTIVO_DESLIGADO = "bot desligado ou reiniciado (atualização ou manutenção)"
MOTIVO_QUEDA_INESPERADA = "queda inesperada (o bot travou ou a VM desligou)"

# Motivos em que o bot NÃO volta sozinho: a permanência foi desativada.
MOTIVOS_DEFINITIVOS = (MOTIVO_SAIR, MOTIVO_SALA_APAGADA)

if not TOKEN:
    raise SystemExit("Defina DISCORD_TOKEN no arquivo .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("Minox")


def agora() -> datetime:
    return datetime.now(timezone.utc)


def atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)  # escrita atômica: não corrompe se cair no meio


# ---------------------------------------------------------------- config ---
class Config:
    """
    Guarda, por servidor, a sala de voz e (opcional) o canal dos avisos.

    config.json:
    {
      "rooms":  {"<id_do_servidor>": <id_da_sala>},
      "avisos": {"<id_do_servidor>": <id_do_canal>}
    }
    Sem "avisos", os avisos de desconexão vão para o chat da própria sala de voz.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rooms: dict[int, int] = {}
        self.notices: dict[int, int] = {}
        try:
            raw = json.loads(path.read_text())
            # Um config.json da versão de servidor único é ignorado (basta usar /definir-sala de novo).
            self.rooms = {int(g): int(c) for g, c in raw.get("rooms", {}).items()}
            self.notices = {int(g): int(c) for g, c in raw.get("avisos", {}).items()}
        except (FileNotFoundError, json.JSONDecodeError, AttributeError, ValueError):
            pass

    def _save(self) -> None:
        atomic_write(self.path, {
            "rooms": {str(g): c for g, c in self.rooms.items()},
            "avisos": {str(g): c for g, c in self.notices.items()},
        })

    def get(self, guild_id: int) -> int | None:
        return self.rooms.get(guild_id)

    def set(self, guild_id: int, channel_id: int) -> None:
        self.rooms[guild_id] = channel_id
        self._save()

    def remove(self, guild_id: int) -> None:
        # O canal de avisos é preservado: vale de novo se /definir-sala for usado outra vez.
        if self.rooms.pop(guild_id, None) is not None:
            self._save()

    def get_notice(self, guild_id: int) -> int | None:
        return self.notices.get(guild_id)

    def set_notice(self, guild_id: int, channel_id: int) -> None:
        self.notices[guild_id] = channel_id
        self._save()

    def remove_notice(self, guild_id: int) -> None:
        if self.notices.pop(guild_id, None) is not None:
            self._save()

    def forget(self, guild_id: int) -> None:
        """Esquece tudo de um servidor (usado quando o bot é removido dele)."""
        sala = self.rooms.pop(guild_id, None)
        aviso = self.notices.pop(guild_id, None)
        if sala is not None or aviso is not None:
            self._save()


# ------------------------------------------------------------- conexões ---
class Sessions:
    """
    Guarda, por servidor, a conexão atual e APENAS a última conexão encerrada.

    conexoes.json:
    {
      "<id_do_servidor>": {
        "atual":  {"inicio": "...", "visto": "...", "sala": <id>} ou null,
        "ultima": {"inicio": "...", "fim": "...", "motivo": "...", "sala": <id>} ou null
      }
    }
    "visto" é atualizado a cada minuto. Se o bot cair sem aviso,
    o fim da conexão é o último "visto" (precisão de ~1 minuto).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict] = {}
        try:
            self.data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        atomic_write(self.path, self.data)

    def _entry(self, guild_id: int) -> dict:
        return self.data.setdefault(str(guild_id), {"atual": None, "ultima": None})

    def current(self, guild_id: int) -> dict | None:
        return self.data.get(str(guild_id), {}).get("atual")

    def last(self, guild_id: int) -> dict | None:
        return self.data.get(str(guild_id), {}).get("ultima")

    def start(self, guild_id: int, channel_id: int | None = None) -> None:
        entry = self._entry(guild_id)
        if entry["atual"] is None:
            now = agora().isoformat()
            entry["atual"] = {"inicio": now, "visto": now, "sala": channel_id}
            self.save()

    def end(self, guild_id: int, motivo: str, fim: str | None = None) -> dict | None:
        """Encerra a conexão atual e devolve o registro, ou None se não havia conexão."""
        entry = self.data.get(str(guild_id))
        if not entry or entry.get("atual") is None:
            return None
        entry["ultima"] = {
            "inicio": entry["atual"]["inicio"],
            "fim": fim or agora().isoformat(),
            "motivo": motivo,
            "sala": entry["atual"].get("sala"),
        }
        entry["atual"] = None
        self.save()
        return entry["ultima"]

    def end_all(self, motivo: str, use_last_seen: bool = False) -> list[tuple[int, dict]]:
        """Encerra tudo e devolve [(id_do_servidor, registro)] do que foi encerrado."""
        encerradas: list[tuple[int, dict]] = []
        for gid, entry in self.data.items():
            atual = entry.get("atual")
            if atual:
                fim = atual["visto"] if use_last_seen else None
                registro = self.end(int(gid), motivo, fim)
                if registro:
                    encerradas.append((int(gid), registro))
        return encerradas

    def heartbeat(self) -> None:
        now = agora().isoformat()
        changed = False
        for entry in self.data.values():
            if entry.get("atual"):
                entry["atual"]["visto"] = now
                changed = True
        if changed:
            self.save()

    def forget(self, guild_id: int) -> None:
        if self.data.pop(str(guild_id), None) is not None:
            self.save()


def duracao(inicio: str, fim: datetime | str) -> str:
    """Formata a duração entre duas datas como '2 d 3 h 15 min'."""
    a = datetime.fromisoformat(inicio)
    b = datetime.fromisoformat(fim) if isinstance(fim, str) else fim
    total_min = int((b - a).total_seconds() // 60)
    if total_min < 1:
        return "menos de 1 min"
    dias, resto = divmod(total_min, 1440)
    horas, minutos = divmod(resto, 60)
    partes = []
    if dias:
        partes.append(f"{dias} d")
    if horas:
        partes.append(f"{horas} h")
    if minutos or not partes:
        partes.append(f"{minutos} min")
    return " ".join(partes)


def data_discord(iso: str) -> str:
    """Data no formato do Discord: aparece no fuso de quem está lendo."""
    ts = int(datetime.fromisoformat(iso).timestamp())
    return f"<t:{ts}:f>"


def embed_desconexao(registro: dict) -> discord.Embed:
    """Resumo de uma conexão encerrada, para avisar no chat."""
    sala = registro.get("sala")
    definitivo = registro["motivo"] in MOTIVOS_DEFINITIVOS
    embed = discord.Embed(
        title="🔌 Desconectado da sala",
        description=f"Saí de <#{sala}>." if sala else None,
        color=discord.Color.light_grey() if definitivo else discord.Color.orange(),
    )
    embed.add_field(
        name="Tempo conectado",
        value=f"**{duracao(registro['inicio'], registro['fim'])}**",
        inline=False,
    )
    embed.add_field(name="Início", value=data_discord(registro["inicio"]), inline=True)
    embed.add_field(name="Fim", value=data_discord(registro["fim"]), inline=True)
    embed.add_field(name="Motivo", value=registro["motivo"], inline=False)
    embed.set_footer(
        text="A permanência foi desativada; use /definir-sala para reativar."
        if definitivo
        else "Já estou voltando sozinho."
    )
    return embed


# ------------------------------------------------------------ cumprimentos ---
# Maiúsculas/minúsculas não importam e a última letra de dia/tarde/noite pode repetir
# ("bom diaaa"). As duas palavras precisam estar na mensagem.
RE_BOM_DIA = re.compile(r"\bbom\s+dia+\b", re.IGNORECASE)
RE_BOA_TARDE = re.compile(r"\bboa\s+tarde+\b", re.IGNORECASE)
RE_BOA_NOITE = re.compile(r"\bboa\s+noite+\b", re.IGNORECASE)


def resposta_cumprimento(texto: str, agora: datetime) -> str | None:
    """Resposta para o cumprimento, se ele combina com o horário; senão None."""
    minutos = agora.hour * 60 + agora.minute
    if 6 * 60 <= minutos < 12 * 60:
        return "Bom dia!" if RE_BOM_DIA.search(texto) else None
    if 12 * 60 <= minutos < 18 * 60:
        return "Boa tarde!" if RE_BOA_TARDE.search(texto) else None
    if not RE_BOA_NOITE.search(texto):
        return None
    # 00:01 às 05:59 o bot "dorme"; 18:00 até 00:00 é boa noite normal.
    if 1 <= minutos < 6 * 60:
        return "Hora do meu descanso, boa noite!"
    return "Boa noite!"


# ------------------------------------------------------------------- bot ---
class Minox(discord.AutoShardedClient):
    def __init__(self) -> None:
        intents = discord.Intents.default()  # guilds + voice_states
        intents.message_content = True  # privilegiada: necessária para ler os cumprimentos
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.config = Config(CONFIG_FILE)
        self.sessions = Sessions(SESSIONS_FILE)
        self._locks: dict[int, asyncio.Lock] = {}
        self._closing = False
        # Avisos que não puderam ser enviados ainda (o bot ainda não estava conectado).
        self._avisos_pendentes: list[tuple[int, dict]] = []

    async def setup_hook(self) -> None:
        # Conexões que ficaram "abertas" no arquivo: o bot caiu sem aviso.
        # O aviso só pode ser enviado depois do on_ready, então fica na fila.
        self._avisos_pendentes = self.sessions.end_all(MOTIVO_QUEDA_INESPERADA, use_last_seen=True)

        # Desligamento pelo systemd (stop/restart): registra o fim antes de sair.
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(self.close()))
        except (NotImplementedError, RuntimeError):
            pass  # Windows não suporta; lá o Ctrl+C já chama close()

        # Comandos globais: valem para todos os servidores, uma única vez cada.
        await self.tree.sync()
        if DEV_GUILD_ID:
            # Remove as cópias antigas registradas só no servidor de testes,
            # que faziam cada comando aparecer duplicado nele.
            dev = discord.Object(id=DEV_GUILD_ID)
            self.tree.clear_commands(guild=dev)
            await self.tree.sync(guild=dev)
            log.info("Cópias duplicadas de comandos removidas do servidor %s.", DEV_GUILD_ID)
        self.watchdog.start()

    async def close(self) -> None:
        if not self._closing:
            self._closing = True
            encerradas = self.sessions.end_all(MOTIVO_DESLIGADO)
            log.info("Desligando; conexões registradas.")
            # Avisa antes de cair, sem travar o desligamento se o Discord demorar.
            try:
                await asyncio.wait_for(self.anunciar_varias(encerradas), timeout=AVISO_TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                log.warning("Não deu tempo de avisar todas as desconexões: %s", exc)
        await super().close()

    def lock_for(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    # ------------------------------------------------------------- avisos ---
    def canal_de_aviso(self, guild: discord.Guild, sala_id: int | None) -> discord.abc.Messageable | None:
        """
        Onde avisar: o canal escolhido em /definir-avisos ou, por padrão,
        o chat embutido da própria sala de voz. None se não há onde escrever.
        """
        if guild.me is None:
            return None
        for channel_id in (self.config.get_notice(guild.id), sala_id):
            if not channel_id:
                continue
            canal = guild.get_channel(channel_id)
            if not isinstance(canal, discord.abc.Messageable):
                continue
            perms = canal.permissions_for(guild.me)  # type: ignore[arg-type]
            if perms.view_channel and perms.send_messages and perms.embed_links:
                return canal
        return None

    async def anunciar_desconexao(self, guild_id: int, registro: dict) -> None:
        """Manda no chat o resumo de uma conexão que acabou de ser encerrada."""
        guild = self.get_guild(guild_id)
        if guild is None:
            return
        canal = self.canal_de_aviso(guild, registro.get("sala"))
        if canal is None:
            log.info("[%s] Sem canal com permissão para avisar a desconexão.", guild.name)
            return
        try:
            await canal.send(embed=embed_desconexao(registro))
        except discord.HTTPException as exc:
            log.warning("[%s] Falha ao avisar a desconexão: %s", guild.name, exc)

    async def anunciar_varias(self, encerradas: list[tuple[int, dict]]) -> None:
        for guild_id, registro in encerradas:
            await self.anunciar_desconexao(guild_id, registro)

    async def encerrar(self, guild_id: int, motivo: str) -> None:
        """Encerra a conexão atual do servidor e avisa no chat, se havia conexão."""
        registro = self.sessions.end(guild_id, motivo)
        if registro:
            await self.anunciar_desconexao(guild_id, registro)

    # ----------------------------------------------------------- conexão ---
    async def ensure_connected(self, guild: discord.Guild) -> None:
        """Garante que o bot está na sala configurada deste servidor."""
        channel_id = self.config.get(guild.id)
        if channel_id is None or self._closing:
            return
        async with self.lock_for(guild.id):
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                log.warning("[%s] Sala %s não existe mais; removendo config.", guild.name, channel_id)
                await self.encerrar(guild.id, MOTIVO_SALA_APAGADA)
                self.config.remove(guild.id)
                return

            vc: discord.VoiceClient | None = guild.voice_client  # type: ignore[assignment]
            try:
                if vc and vc.is_connected():
                    if vc.channel.id != channel.id:
                        await vc.move_to(channel)
                        log.info("[%s] Movido de volta para #%s", guild.name, channel.name)
                    self.sessions.start(guild.id, channel.id)
                    return
                if vc:  # conexão quebrada: registra, avisa e limpa antes de reconectar
                    await self.encerrar(guild.id, MOTIVO_QUEDA_VOZ)
                    await vc.disconnect(force=True)
                elif self.sessions.current(guild.id):
                    await self.encerrar(guild.id, MOTIVO_QUEDA_VOZ)
                await channel.connect(self_deaf=True, self_mute=True, reconnect=True, timeout=30)
                self.sessions.start(guild.id, channel.id)
                log.info("[%s] Conectado em #%s", guild.name, channel.name)
            except Exception as exc:  # noqa: BLE001
                log.error("[%s] Falha ao conectar: %s", guild.name, exc)

    async def ensure_all(self) -> None:
        for guild_id in list(self.config.rooms):
            guild = self.get_guild(guild_id)
            if guild is None:
                continue  # shard ainda conectando ou bot removido
            await self.ensure_connected(guild)
            await asyncio.sleep(STAGGER)  # evita rajadas de conexão (rate limit)

    @tasks.loop(seconds=CHECK_INTERVAL)
    async def watchdog(self) -> None:
        self.sessions.heartbeat()
        await self.ensure_all()

    @watchdog.before_loop
    async def before_watchdog(self) -> None:
        await self.wait_until_ready()
        # Agora que há conexão com o Discord, avisa as quedas detectadas ao iniciar.
        pendentes, self._avisos_pendentes = self._avisos_pendentes, []
        await self.anunciar_varias(pendentes)

    # ------------------------------------------------------------ eventos ---
    async def on_ready(self) -> None:
        log.info("Logado como %s em %d servidor(es), %d shard(s).",
                 self.user, len(self.guilds), self.shard_count or 1)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        resposta = resposta_cumprimento(message.content, datetime.now(FUSO))
        if resposta is None:
            return
        try:
            await message.reply(resposta, mention_author=True)
        except discord.HTTPException:
            log.warning("[%s] Sem permissão para responder em #%s.", message.guild.name, message.channel)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("Removido do servidor %s; apagando config.", guild.name)
        self.config.forget(guild.id)
        self.sessions.forget(guild.id)
        self._locks.pop(guild.id, None)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if self.config.get_notice(channel.guild.id) == channel.id:
            log.info("[%s] Canal de avisos foi apagado; voltando ao chat da sala.", channel.guild.name)
            self.config.remove_notice(channel.guild.id)
        if self.config.get(channel.guild.id) == channel.id:
            log.info("[%s] Sala configurada foi apagada.", channel.guild.name)
            await self.encerrar(channel.guild.id, MOTIVO_SALA_APAGADA)
            self.config.remove(channel.guild.id)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        # Reage só quando o próprio bot foi tirado/movido da sala.
        if member.id != self.user.id or self._closing:
            return
        channel_id = self.config.get(member.guild.id)
        if channel_id is None:
            return
        if after.channel is None or after.channel.id != channel_id:
            motivo = MOTIVO_DESCONECTADO if after.channel is None else MOTIVO_MOVIDO
            await self.encerrar(member.guild.id, motivo)
            log.info("[%s] %s; voltando em %ss.", member.guild.name, motivo, RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)
            await self.ensure_connected(member.guild)


bot = Minox()


# -------------------------------------------------------------- comandos ---
def only_admins():
    async def predicate(interaction: discord.Interaction) -> bool:
        perms = interaction.user.guild_permissions  # type: ignore[union-attr]
        if not perms.administrator:
            raise app_commands.CheckFailure("Apenas administradores podem usar este comando.")
        return True
    return app_commands.check(predicate)


@bot.tree.command(name="definir-sala", description="Escolhe a sala de voz onde o Minox vai ficar.")
@app_commands.describe(sala="Sala de voz onde o bot deve permanecer")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@only_admins()
async def definir_sala(interaction: discord.Interaction, sala: discord.VoiceChannel) -> None:
    guild = interaction.guild
    perms = sala.permissions_for(guild.me)  # type: ignore[union-attr]
    if not (perms.view_channel and perms.connect):
        await interaction.response.send_message(
            f"Não tenho permissão para entrar em {sala.mention}. "
            "Dê ao Minox *Ver canal* e *Conectar* nessa sala.",
            ephemeral=True,
        )
        return

    bot.config.set(guild.id, sala.id)  # type: ignore[union-attr]
    await interaction.response.send_message(f"Combinado! Vou ficar em {sala.mention} 24h. ✅", ephemeral=True)
    await bot.ensure_connected(guild)  # type: ignore[arg-type]


@bot.tree.command(name="sair", description="Tira o Minox da sala e desativa a permanência.")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@only_admins()
async def sair(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    await bot.encerrar(guild.id, MOTIVO_SAIR)  # type: ignore[union-attr]
    bot.config.remove(guild.id)  # type: ignore[union-attr]
    async with bot.lock_for(guild.id):  # type: ignore[union-attr]
        vc = guild.voice_client  # type: ignore[union-attr]
        if vc:
            await vc.disconnect(force=True)
    await interaction.response.send_message("Saí da sala. Use /definir-sala para me reativar.", ephemeral=True)


@bot.tree.command(
    name="definir-avisos",
    description="Escolhe onde o Minox avisa as desconexões. Sem canal, usa o chat da própria sala.",
)
@app_commands.describe(canal="Canal dos avisos; deixe em branco para usar o chat da sala de voz")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@only_admins()
async def definir_avisos(interaction: discord.Interaction, canal: discord.TextChannel | None = None) -> None:
    guild = interaction.guild
    if canal is None:
        bot.config.remove_notice(guild.id)  # type: ignore[union-attr]
        await interaction.response.send_message(
            "Pronto! Vou avisar as desconexões no chat da própria sala de voz.", ephemeral=True
        )
        return

    perms = canal.permissions_for(guild.me)  # type: ignore[union-attr]
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        await interaction.response.send_message(
            f"Não consigo escrever em {canal.mention}. "
            "Dê ao Minox *Ver canal*, *Enviar mensagens* e *Inserir links* nesse canal.",
            ephemeral=True,
        )
        return

    bot.config.set_notice(guild.id, canal.id)  # type: ignore[union-attr]
    await interaction.response.send_message(f"Combinado! Vou avisar as desconexões em {canal.mention}. ✅", ephemeral=True)


@bot.tree.command(name="status", description="Mostra em qual sala o Minox está configurado.")
@app_commands.guild_only()
async def status(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    channel_id = bot.config.get(guild.id)  # type: ignore[union-attr]
    aviso_id = bot.config.get_notice(guild.id)  # type: ignore[union-attr]
    if channel_id is None:
        msg = "Nenhuma sala definida neste servidor. Um administrador pode usar /definir-sala."
    else:
        vc = guild.voice_client  # type: ignore[union-attr]
        conectado = "conectado ✅" if vc and vc.is_connected() else "reconectando… ⏳"
        avisos = f"<#{aviso_id}>" if aviso_id else f"chat da sala (<#{channel_id}>)"
        msg = f"Sala definida: <#{channel_id}> — {conectado}\nAvisos de desconexão: {avisos}"
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="tempo-online", description="Mostra há quanto tempo o Minox está na sala e como foi a última conexão.")
@app_commands.guild_only()
async def tempo_online(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    channel_id = bot.config.get(guild.id)  # type: ignore[union-attr]
    vc = guild.voice_client  # type: ignore[union-attr]
    online = bool(vc and vc.is_connected() and channel_id and vc.channel.id == channel_id)
    atual = bot.sessions.current(guild.id)  # type: ignore[union-attr]
    ultima = bot.sessions.last(guild.id)  # type: ignore[union-attr]

    embed = discord.Embed(
        title="⏱️ Tempo online",
        color=discord.Color.green() if online else discord.Color.red(),
    )

    # Status
    if channel_id is None:
        status_txt = "⚪ Offline — nenhuma sala definida (use /definir-sala)\n"
    elif online:
        status_txt = f"🟢 Online em <#{channel_id}>\n"
    else:
        status_txt = f"🔴 Offline — reconectando em <#{channel_id}>\n"
    embed.add_field(name="Status", value=status_txt, inline=False)

    # Conexão atual
    if online and atual:
        embed.add_field(
            name="Tempo de conexão",
            value=f"**{duracao(atual['inicio'], agora())}**\nDesde {data_discord(atual['inicio'])}",
            inline=False,
        )
    else:
        embed.add_field(name="Tempo de conexão", value="Sem conexão no momento", inline=False)

    # Última conexão encerrada
    if ultima:
        embed.add_field(
            name="Última conexão",
            value=(
                f"**{duracao(ultima['inicio'], ultima['fim'])}**\n"
                f"Início: {data_discord(ultima['inicio'])}\n"
                f"Fim: {data_discord(ultima['fim'])}\n"
                f"Motivo: {ultima['motivo']}"
            ),
            inline=False,
        )
    else:
        embed.add_field(name="Última conexão", value="Nenhuma conexão encerrada registrada ainda", inline=False)

    embed.set_footer(text="Datas no seu fuso horário. Registro da última conexão encerrada.")
    await interaction.response.send_message(embed=embed)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    text = str(error) if isinstance(error, app_commands.CheckFailure) else "Algo deu errado. Veja os logs."
    if not isinstance(error, app_commands.CheckFailure):
        log.exception("Erro em comando", exc_info=error)
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)
