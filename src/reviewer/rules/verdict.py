"""Ce que l'agent REND au runner, fil par fil.

── POURQUOI L'AGENT NE REPOND PAS LUI-MEME ─────────────────────────────────

Meme raison que le push. Repondre dans un fil et le resoudre sont des gestes
VISIBLES par les autres : ils retirent une remarque du compteur qui retient le
merge. Les laisser a l'agent obligerait a lui donner un jeton d'ecriture sur la
forge, donc a compter sur une regle de prompt pour qu'il ne s'en serve pas
ailleurs. Ici, il ne l'a simplement pas — il rend un verdict, le runner ecrit.

Consequence utile : une injection reussie dans une remarque ne peut pas faire
poster quoi que ce soit hors des fils qu'on a nous-memes soumis (cf. `parse`,
qui refuse tout identifiant qu'on n'a pas envoye).

── POURQUOI UN SCHEMA, PAS DU TEXTE A RELIRE ───────────────────────────────

Le SDK sait contraindre la sortie a un schema JSON (`output_format`) et rend
l'objet valide dans `ResultMessage.structured_output`. Parser de la prose
serait un analyseur de plus a maintenir, qui echouerait le jour ou l'agent
choisit une autre tournure — et son echec se lirait comme « aucun fil traite ».

── LA POLARITE DU DOUTE ────────────────────────────────────────────────────

Un verdict illisible, absent, ou qui cite un fil qu'on n'a pas soumis ne RESOUT
RIEN et demande un arbitrage. Le doute penche toujours du cote de l'humain :
resoudre a tort retire une remarque que personne ne reverra, alors que demander
a tort coute une notification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = ["Issue", "SCHEMA", "ThreadVerdict", "Verdict", "parse"]


class Issue(str, Enum):
    """Ce que l'agent a fait d'une remarque.

    Trois valeurs, et une seule referme le fil. C'est deliberé : `REFUTE` et
    `ARBITRAGE` laissent tous deux le fil OUVERT et appellent l'humain, parce
    que resoudre un fil sur lequel on n'est pas d'accord revient a clore le
    debat en sa faveur.
    """

    CORRIGE = "corrige"      # traite dans le code -> repondre ET resoudre
    REFUTE = "refute"        # la remarque est fausse -> repondre, laisser OUVERT
    ARBITRAGE = "arbitrage"  # il faut une decision humaine -> repondre, OUVERT

    @property
    def resolves(self) -> bool:
        return self is Issue.CORRIGE

    @property
    def needs_human(self) -> bool:
        """Ce verdict appelle-t-il l'humain ?

        `REFUTE` en fait partie. Un desaccord non signale se transforme en fil
        ouvert dont personne ne sait qu'il attend quelqu'un — et un fil ouvert
        retient le merge sur les depots de Damien.
        """
        return self is not Issue.CORRIGE


# Schema passe au SDK via `output_format`. Volontairement PLAT et court : un
# schema profond multiplie les facons pour le modele de le remplir a moitie, et
# chaque champ facultatif est un cas de plus a gerer ici.
SCHEMA: dict = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "threads"],
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "Une a trois phrases : ce qui a ete change, et pourquoi. "
                    "Sert de message de commit et de resume dans le journal."
                ),
            },
            "threads": {
                "type": "array",
                "description": (
                    "Un objet par fil soumis. Aucun fil ne doit etre omis : un "
                    "fil absent est traite comme demandant un arbitrage."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["thread_id", "outcome", "reply"],
                    "properties": {
                        "thread_id": {
                            "type": "string",
                            "description": "Identifiant du fil, repris tel quel du prompt.",
                        },
                        "outcome": {
                            "type": "string",
                            "enum": ["corrige", "refute", "arbitrage"],
                            "description": (
                                "corrige : traite dans le code, le fil peut etre "
                                "referme. refute : la remarque est fausse, "
                                "demonstration a l'appui. arbitrage : une "
                                "decision humaine est necessaire."
                            ),
                        },
                        "reply": {
                            "type": "string",
                            "description": (
                                "Le texte publie DANS le fil, en francais, lisible "
                                "par un humain. Dire ce qui a ete change et ou, ou "
                                "pourquoi rien ne l'a ete. Pas de salutations."
                            ),
                        },
                    },
                },
            },
            "blocked": {
                "type": "string",
                "description": (
                    "A remplir UNIQUEMENT si le travail n'a pas pu etre mene : "
                    "ce qui bloque, en une phrase. Laisser vide sinon."
                ),
            },
        },
    },
}


@dataclass(frozen=True, slots=True)
class ThreadVerdict:
    thread_id: str
    outcome: Issue
    reply: str


@dataclass(frozen=True, slots=True)
class Verdict:
    summary: str = ""
    threads: tuple[ThreadVerdict, ...] = ()
    blocked: str = ""
    # Ce que le parseur a du corriger. RENDU, jamais avale : un verdict repare
    # en silence donnerait l'illusion d'un agent qui repond bien.
    anomalies: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        return not self.blocked and bool(self.threads)

    @property
    def needs_human(self) -> bool:
        return bool(self.blocked) or any(t.outcome.needs_human for t in self.threads)

    def for_thread(self, thread_id: str) -> ThreadVerdict | None:
        for t in self.threads:
            if t.thread_id == thread_id:
                return t
        return None

    def commit_subject(self, repo: str, cycle: int) -> str:
        """Sujet de commit conventionnel, tire du resume de l'agent.

        Le depot impose Conventional Commits, et `commit_all` refuse un sujet
        qui n'en est pas un. On prefixe donc nous-memes plutot que de demander
        la forme a l'agent : une exigence de format posee dans un prompt est
        respectee la plupart du temps, et « la plupart du temps » veut dire un
        job perdu de temps en temps.
        """
        texte = " ".join((self.summary or "").split())
        if not texte:
            texte = f"retours de revue (cycle {cycle})"
        # Un sujet de commit tient sur une ligne courte ; le detail va dans le
        # fil de revue, qui est l'endroit ou on le relira.
        if len(texte) > 68:
            texte = texte[:67].rstrip() + "…"
        return f"fix({repo}): {texte[0].lower()}{texte[1:]}"

    def commit_message(self, repo: str, cycle: int, *,
                       issue: int | None = None) -> str:
        """Message complet : sujet conventionnel, puis la reference a l'issue.

        ── « Refs », JAMAIS « Fixes », DANS UN COMMIT ──────────────────────

        GitHub ferme une issue des qu'un commit portant `Fixes #<n>` atteint la
        branche par defaut. Sur une issue qui s'acheve par une OPERATION —
        migration a lancer, backfill en production, reglage a couper chez un
        prestataire — elle se fermerait au deploiement du code, avant que le
        geste soit fait, et le tableau annoncerait « termine » a tort.

        Le mot-cle de liaison a sa place dans la description de la PR, ou il ne
        declenche qu'au merge et sous le controle de qui relit. Ici, on
        REFERENCE : le lien est visible, la fermeture reste une decision.
        """
        sujet = self.commit_subject(repo, cycle)
        if issue is None:
            return sujet
        return f"{sujet}\n\nRefs #{issue}\n"


def _texte(valeur: object, defaut: str = "") -> str:
    return valeur.strip() if isinstance(valeur, str) else defaut


def parse(brut: object, *, submitted: tuple[str, ...] | list[str]) -> Verdict:
    """Lit la sortie structuree de l'agent. Ne leve JAMAIS.

    `submitted` est la liste des fils qu'on a soumis. Elle est la garde : un
    identifiant absent de cette liste est REFUSE, pas ignore poliment. C'est ce
    qui empeche une remarque piegee de faire resoudre un fil auquel l'agent
    n'avait pas affaire — la seule ecriture que le prompt pourrait detourner.

    Un fil soumis mais ABSENT du verdict est ajoute en `ARBITRAGE`. Le silence
    n'est pas un accord : un fil oublie qu'on laisserait tomber disparaitrait du
    compteur au prochain passage, curseur avance, sans que personne ne l'ait lu.
    """
    attendus = list(dict.fromkeys(submitted))
    anomalies: list[str] = []

    if not isinstance(brut, dict):
        return Verdict(
            summary="",
            threads=tuple(ThreadVerdict(i, Issue.ARBITRAGE, "") for i in attendus),
            blocked="l'agent n'a rendu aucune sortie structuree exploitable",
            anomalies=(f"sortie de type {type(brut).__name__}, attendu un objet",),
        )

    vus: dict[str, ThreadVerdict] = {}
    for i, element in enumerate(brut.get("threads") or []):
        if not isinstance(element, dict):
            anomalies.append(f"entree {i} : {type(element).__name__} au lieu d'un objet")
            continue
        tid = _texte(element.get("thread_id"))
        if tid not in attendus:
            # Refus, pas correction : on ne devine pas de quel fil il s'agissait.
            anomalies.append(f"fil « {tid[:40]} » non soumis — verdict ignore")
            continue
        try:
            issue = Issue(_texte(element.get("outcome")).lower())
        except ValueError:
            anomalies.append(
                f"fil {tid[:12]}… : verdict « {_texte(element.get('outcome'))[:20]} » "
                "inconnu — traite comme demandant un arbitrage"
            )
            issue = Issue.ARBITRAGE
        reponse = _texte(element.get("reply"))
        if not reponse:
            # Une resolution sans un mot d'explication laisse la remarque
            # disparaitre sans trace lisible. On ne resout pas la-dessus.
            anomalies.append(f"fil {tid[:12]}… : reponse vide — bascule en arbitrage")
            issue = Issue.ARBITRAGE
        if tid in vus:
            anomalies.append(f"fil {tid[:12]}… : cite deux fois — le premier verdict tient")
            continue
        vus[tid] = ThreadVerdict(tid, issue, reponse)

    for tid in attendus:
        if tid not in vus:
            anomalies.append(f"fil {tid[:12]}… : absent du verdict — arbitrage")
            vus[tid] = ThreadVerdict(tid, Issue.ARBITRAGE, "")

    return Verdict(
        summary=_texte(brut.get("summary")),
        # L'ordre de SOUMISSION, pas celui du verdict : c'est celui du prompt,
        # donc celui que l'humain relira.
        threads=tuple(vus[i] for i in attendus),
        blocked=_texte(brut.get("blocked")),
        anomalies=tuple(anomalies),
    )
