"""Lance les verifications d'un depot dans un worktree, et rend un verdict.

Les commandes viennent du PROFIL, ecrit par l'humain — pas de la forge. C'est ce
qui autorise `shell=True` : la ligne `npm run lint` a besoin d'un shell pour
resoudre `npm.cmd` sous Windows, et le risque d'injection n'existe pas puisque
rien de ce qui vient de GitHub n'entre ici. Si un jour une commande devait etre
composee a partir d'une donnee externe, cette hypothese tomberait — d'ou le
rappel explicite.

── DEUX DECISIONS QUI CHANGENT CE QU'ON APPREND D'UN ECHEC ─────────────────

1. ON S'ARRETE A LA PREMIERE COMMANDE ROUGE. Enchainer coute des minutes et
   n'apprend rien : un lint casse produit des tests qui echouent pour la meme
   raison. Mais les commandes NON LANCEES sont RENDUES — un « 1 echec » qui
   cache « 3 non lancees » se lit comme une couverture complete.

2. ON GARDE LA TETE ET LA QUEUE DE LA SORTIE. Selon l'outil, l'information
   utile est a un bout ou a l'autre : pytest resume a la fin, eslint enumere au
   fil. Ne garder que la queue perdrait la moitie des diagnostics.
"""

from __future__ import annotations

import locale
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["CheckOutcome", "CheckReport", "environnement", "outils_locaux",
           "run_checks"]

# Assez pour un resume de pytest ou une liste d'erreurs eslint, pas assez pour
# noyer un journal.
_TETE = 2000
_QUEUE = 4000


def _decoder(brut: bytes | str | None) -> str:
    """Decode une sortie de commande sans en perdre les accents.

    On capture des OCTETS et on essaie plusieurs encodages, parce qu'il n'y en a
    pas un seul sur ce poste : Node et les outils modernes ecrivent en UTF-8, la
    console Windows et les outils qui s'y fient ecrivent en cp1252. Imposer
    UTF-8 transforme « raté » en « rat<?> », et une sortie de test illisible est
    un diagnostic perdu — d'autant que les 4890 tests du backend portent des
    noms francais.

    On force aussi UTF-8 cote fils quand c'est possible (`PYTHONUTF8`), ce qui
    rend le cas courant propre ; ce repli couvre le reste.
    """
    if brut is None:
        return ""
    if isinstance(brut, str):
        return brut
    for encodage in ("utf-8", locale.getpreferredencoding(False), "cp1252"):
        try:
            return brut.decode(encodage)
        except (UnicodeDecodeError, LookupError):
            continue
    return brut.decode("utf-8", "replace")


def _tronquer(texte: str) -> str:
    texte = texte.strip()
    if len(texte) <= _TETE + _QUEUE:
        return texte
    coupe = len(texte) - _TETE - _QUEUE
    return f"{texte[:_TETE]}\n\n… [{coupe} caracteres coupes] …\n\n{texte[-_QUEUE:]}"


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    command: str
    ok: bool
    returncode: int | None
    duration_s: float
    output: str = ""
    timed_out: bool = False
    error: str | None = None      # la commande n'a pas pu demarrer

    def tail(self, lignes: int = 20, max_chars: int = 1500) -> str:
        """La FIN de la sortie — la ou les outils mettent leur verdict.

        ── POURQUOI CE CHAMP N'ETAIT NULLE PART ────────────────────────────

        `output` etait capture et jamais rendu : ni journalise, ni publie. Le
        29/08/2026, un `pytest` sorti en code 2 a produit exactement une ligne
        exploitable — « ECHEC (code 2) » — et il a fallu rejouer la commande a
        la main pour decouvrir que le worktree n'avait pas de `.env`, que la
        collecte avait echoue sur 336 fichiers, et que le code livre n'y etait
        pour rien.

        La FIN plutot que le debut : pytest, npm, cargo et make y placent leur
        resume. Un extrait de tete montre l'echauffement.
        """
        texte = (self.output or "").strip()
        if not texte:
            return ""
        garde = "\n".join(texte.splitlines()[-lignes:])
        if len(garde) > max_chars:
            # On coupe par la GAUCHE : la derniere ligne est celle qui conclut.
            garde = "…\n" + garde[-max_chars:]
        return garde

    @property
    def summary(self) -> str:
        if self.error:
            return f"{self.command} — n'a pas demarre : {self.error}"
        if self.timed_out:
            return f"{self.command} — TIMEOUT apres {self.duration_s:.0f} s"
        if self.outil_absent:
            # NOMME, parce que « ECHEC (code 127) » se lit comme un lint rouge
            # et envoie chercher un bug dans le code livre. Mesure du
            # 30/08/2026 sur `backend#748` : `ruff` n'etait pas installe dans
            # le conteneur, et le rapport disait « verifications rouges ».
            return (f"{self.command} — OUTIL INTROUVABLE (code 127) : le depot "
                    "n'est pas outille, ce n'est pas le code qui echoue")
        etat = "vert" if self.ok else f"ECHEC (code {self.returncode})"
        return f"{self.command} — {etat} en {self.duration_s:.1f} s"

    @property
    def outil_absent(self) -> bool:
        """127 = le shell n'a pas trouve la commande (`command not found`).

        Ce n'est pas un echec de verification : c'est une capacite qui manque.
        Les deux demandent des gestes opposes — corriger du code, ou installer
        un outil — d'ou la distinction jusque dans le resume.
        """
        return self.returncode == 127


@dataclass(frozen=True, slots=True)
class CheckReport:
    outcomes: tuple[CheckOutcome, ...] = ()
    not_run: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """Vert seulement si TOUT a tourne et tout est passe.

        Des commandes non lancees rendent le rapport rouge, jamais vert : un
        verdict rendu sur une couverture partielle est un verdict faux.
        """
        return bool(self.outcomes) and not self.not_run and all(o.ok for o in self.outcomes)

    @property
    def failure(self) -> CheckOutcome | None:
        return next((o for o in self.outcomes if not o.ok), None)

    def summary(self) -> str:
        if not self.outcomes and not self.not_run:
            return "aucune verification configuree pour ce depot"
        verts = sum(1 for o in self.outcomes if o.ok)
        bilan = f"{verts}/{len(self.outcomes)} verification(s) verte(s)"
        if self.not_run:
            # Une coupe qui ne se journalise pas se lit comme une couverture
            # complete.
            bilan += f" — {len(self.not_run)} non lancee(s) : {', '.join(self.not_run)}"
        if (e := self.failure) is not None:
            bilan += f" — arret sur : {e.summary}"
        return bilan


def outils_locaux(cwd: Path) -> list[Path]:
    """Repertoires d'outils du depot, a mettre en tete du PATH.

    Un profil ecrit `ruff check .` et `npm run lint` — la forme que documentent
    les conventions du depot, et celle qu'un humain tape. Elle suppose le venv
    ACTIVE et `node_modules/.bin` accessible. Sans cette preparation, la
    commande echoue avec « n'est pas reconnu », ce qui ressemble a un depot
    casse alors que c'est l'environnement qui manque.

    On reproduit donc ce que fait une activation, plutot que d'exiger du profil
    des chemins qualifies que personne n'ecrit a la main.

    Dans un worktree, `.venv` et `node_modules` sont les JONCTIONS montees vers
    le depot principal : la meme regle y vaut sans rien de plus.
    """
    out: list[Path] = []
    for venv in (cwd / ".venv", cwd / "venv"):
        for scripts in (venv / "Scripts", venv / "bin"):
            if scripts.is_dir():
                out.append(scripts)
    if (nb := cwd / "node_modules" / ".bin").is_dir():
        out.append(nb)
    return out


def environnement(scrub: tuple[str, ...] | list[str], cwd: Path) -> dict[str, str]:
    """Environnement des verifications, sans les variables sensibles.

    `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` du SDK exige bubblewrap depuis la
    v2.1.220 et n'existe donc pas sous Windows. On nettoie nous-memes, ce qui
    couvre au moins les sous-processus qu'on lance directement.
    """
    env = dict(os.environ)
    for nom in scrub:
        env.pop(nom, None)

    outils = outils_locaux(cwd)
    if outils:
        env["PATH"] = os.pathsep.join([*(str(p) for p in outils), env.get("PATH", "")])
        # Certains outils lisent `VIRTUAL_ENV` plutot que le PATH.
        for p in outils:
            if p.parent.name in (".venv", "venv"):
                env["VIRTUAL_ENV"] = str(p.parent)
                break
    # Les outils qui detectent la CI changent de sortie (couleurs, progression).
    # Une sortie stable est une sortie qu'on sait lire dans un journal.
    env.setdefault("CI", "1")
    env.setdefault("NO_COLOR", "1")
    # Rend la sortie des fils Python en UTF-8 quel que soit l'encodage de la
    # console. Ne couvre pas tout — d'ou le repli de `_decoder` — mais rend le
    # cas courant propre plutot que reparé apres coup.
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def run_checks(
    commands: list[str] | tuple[str, ...],
    *,
    cwd: Path,
    timeout_s: float = 900.0,
    scrub_env: tuple[str, ...] | list[str] = (),
    on_result=None,
) -> CheckReport:
    """Lance les verifications dans l'ordre, s'arrete a la premiere rouge.

    `on_result` est appele apres chaque commande — c'est ce qui permet au
    journal de montrer l'avancement plutot qu'un silence de plusieurs minutes
    suivi d'un verdict.
    """
    cwd = Path(cwd)
    if not cwd.is_dir():
        raise FileNotFoundError(f"repertoire de travail introuvable : {cwd}")

    env = environnement(scrub_env, cwd)
    resultats: list[CheckOutcome] = []
    restantes = list(commands)

    while restantes:
        commande = restantes.pop(0)
        debut = time.monotonic()
        try:
            # `text=False` : on capture des OCTETS et on decode nous-memes.
            # Laisser subprocess decoder imposerait un seul encodage, alors que
            # Node ecrit en UTF-8 et la console Windows en cp1252.
            r = subprocess.run(
                commande, cwd=str(cwd), shell=True, capture_output=True,
                timeout=timeout_s, env=env,
            )
        except subprocess.TimeoutExpired as e:
            sortie = _decoder(e.stdout) + _decoder(e.stderr)
            issue = CheckOutcome(commande, False, None, time.monotonic() - debut,
                                 _tronquer(sortie), timed_out=True)
        except OSError as e:
            issue = CheckOutcome(commande, False, None, time.monotonic() - debut,
                                 error=str(e))
        else:
            issue = CheckOutcome(commande, r.returncode == 0, r.returncode,
                                 time.monotonic() - debut,
                                 _tronquer(_decoder(r.stdout) + _decoder(r.stderr)))

        resultats.append(issue)
        if on_result is not None:
            on_result(issue)
        if not issue.ok:
            # Arret : enchainer coute des minutes et n'apprend rien de plus.
            return CheckReport(tuple(resultats), tuple(restantes))

    return CheckReport(tuple(resultats))
