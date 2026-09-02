"""Ce qu'il y a a faire sur une PR — fonction PURE de son etat courant.

Aucun appel reseau ici, aucune ecriture, aucune horloge implicite : l'instant
est un parametre. C'est la seule partie du demon qui porte des REGLES, donc la
seule qui merite d'etre testee exhaustivement — et la seule qui peut l'etre sans
un seul stub.

Le systeme est DECLENCHE SUR NIVEAU, pas sur front : on ne reagit pas a un
evenement, on recalcule ce qu'il faudrait faire au vu de l'etat present. Un
reveil manque ne coute donc que de la latence, jamais du travail perdu ; un
webhook recu deux fois ne produit rien de plus la seconde fois.

── CE QUE LA MESURE A IMPOSE ────────────────────────────────────────────────

Trois faits, releves sur douze PR reelles, contredisent la conception naive :

1. Les relecteurs automatiques n'emettent PAS `CHANGES_REQUESTED`. Toutes les
   revues Codex observees sont a l'etat `COMMENTED`. Attendre cet etat, c'est
   attendre un evenement qui n'arrive jamais. Le signal exploitable, ce sont les
   FILS DE REVUE ouverts.

2. La revue n'est pas garantie : 2 PR sur 12 n'en ont recu aucune. Il faut donc
   une borne de TEMPS (`review_window`), pas seulement une borne de cycles —
   sinon une PR sans relecteur attend indefiniment.

3. La severite est un BADGE dans le corps du commentaire (`![P1 Badge]`), pas un
   champ d'API. On la lit dans le texte, avec la prudence que ca impose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

__all__ = [
    "AGENT_MARK",
    "ASK_MARK",
    "BRANCHES_PARTAGEES",
    "DEFAULT_IGNORED_CHECKS",
    "Action",
    "Check",
    "Comment",
    "Decision",
    "PullSnapshot",
    "Severity",
    "State",
    "Thread",
    "compile_ignored",
    "decide",
    "normalise_login",
    "severite_dominante",
    "severity_of",
]

# ── QUI A ECRIT CE COMMENTAIRE ? ────────────────────────────────────────────
#
# Le demon publie sous un JETON, donc sous le compte qui l'a emis — celui de
# Damien, en pratique. Ses propres reponses portent donc le login d'un auteur de
# CONFIANCE, et sans marqueur elles se relanceraient elles-memes : l'agent
# repond, se voit repondre, repond encore.
#
# Reconnaitre ses ecrits par le login serait donc faux. On les marque, avec un
# commentaire HTML invisible au rendu GitHub mais present dans le corps que
# l'API rend. Le marqueur survit a un changement de compte, de jeton et de nom
# d'organisation — un login, non.
AGENT_MARK = "<!-- agent-runner:reponse -->"

# Second marqueur, porte par les seules reponses qui POSENT UNE QUESTION. Il dit
# « la balle est dans le camp de l'humain » : tant qu'il est le dernier mot du
# fil, le fil n'est pas du travail, c'est une attente. Sans lui, un arret pour
# ambiguite reposerait la meme question a chaque passage.
ASK_MARK = "<!-- agent-runner:attente-humain -->"


# Les branches qu'on ne travaille JAMAIS directement. Elles etaient enumerees a
# la main dans six fichiers, et pas avec le meme contenu : `production` etait
# refusee a la configuration mais absente des trois couches qui appliquent —
# donc une branche nommee `production` aurait ete commitee sans broncher. Une
# regle de securite recopiee est une regle qui divergera.
BRANCHES_PARTAGEES: frozenset[str] = frozenset({
    "dev", "main", "master", "production",
})


class State(str, Enum):
    """Sept etats. Un seul est reellement STOCKE : `AGENT_WORKING`.

    Tous les autres se recalculent depuis la forge. Stocker un etat deductible,
    c'est creer une seconde verite qui finira par diverger de la premiere.
    """

    IDLE = "IDLE"                          # rien a faire
    AGENT_WORKING = "AGENT_WORKING"        # un bail est detenu (etat local)
    WAITING_CI = "WAITING_CI"              # un check requis n'a pas conclu
    WAITING_REVIEW = "WAITING_REVIEW"      # checks verts, on laisse venir la revue
    NEEDS_FIX = "NEEDS_FIX"                # du travail identifie, cadre
    READY_FOR_HUMAN = "READY_FOR_HUMAN"    # plus rien a faire : le merge est humain
    NEEDS_HUMAN = "NEEDS_HUMAN"            # arret : ambigu, epuise, ou dangereux


class Action(str, Enum):
    """Effet de bord que la decision AUTORISE. Elle n'en execute aucun."""

    NONE = "none"
    RUN_AGENT = "run_agent"          # prendre le bail, ouvrir un worktree, coder
    NUDGE_REVIEW = "nudge_review"    # relancer le relecteur, une seule fois
    LABEL_READY = "label_ready"
    LABEL_NEEDS_HUMAN = "label_needs_human"
    PROPOSE_DEBT = "propose_debt"    # remarques mineures : proposer, pas creer
    ASK_HUMAN = "ask_human"          # poser la question DANS le fil, et attendre


class Severity(int, Enum):
    P1 = 1
    P2 = 2
    P3 = 3
    UNKNOWN = 99  # non etiquete : traite comme bloquant, cf. `blocking`

    @property
    def blocking(self) -> bool:
        """Une remarque non etiquetee bloque-t-elle ? Oui.

        Le doute penche du cote de l'humain : une remarque qu'on ne sait pas
        classer est traitee comme importante. L'inverse — ignorer ce qu'on n'a
        pas su lire — transformerait chaque evolution du format de badge en
        remarques silencieusement perdues.
        """
        return self is not Severity.P3


def severite_dominante(threads: "tuple[Thread, ...] | list[Thread]") -> Severity | None:
    """La severite qui doit gouverner le traitement d'un lot de remarques.

    Un job traite plusieurs fils d'un coup ; il faut donc UNE severite pour
    choisir le moteur. On prend la plus exigeante, avec un ordre qui suit
    `blocking` plutot que la valeur numerique : `UNKNOWN` vaut 99 mais bloque,
    et un tri naif le classerait derriere `P3` — donc une remarque qu'on n'a pas
    su lire serait traitee comme du nommage, exactement l'inverse de ce que
    `blocking` decide ailleurs.

    Entre bloquants, l'ordre numerique reprend : P1 avant P2 avant UNKNOWN. Une
    remarque non etiquetee est importante, pas prioritaire — la traiter comme un
    P1 ferait payer le moteur le plus cher a chaque evolution du format de
    badge.
    """
    if not threads:
        return None
    return min((t.severity for t in threads),
               key=lambda s: (not s.blocking, s.value))


# `![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)`
_BADGE_RE = re.compile(r"!\[\s*P([123])\s+Badge\s*\]", re.IGNORECASE)
# Repli : « **P2** » ou « [P2] » en tete de remarque.
_TETE_RE = re.compile(r"^\W{0,8}\**\[?\s*P([123])\s*\]?\**", re.IGNORECASE)


def severity_of(body: str) -> Severity:
    """Severite d'une remarque, lue dans son TEXTE.

    Deux formes, dans cet ordre : le badge d'image, puis un marqueur en tete.
    Aucune des deux ne matche -> `UNKNOWN`, qui bloque (cf. `Severity.blocking`).
    """
    if m := _BADGE_RE.search(body or ""):
        return Severity(int(m.group(1)))
    if m := _TETE_RE.match((body or "").lstrip()):
        return Severity(int(m.group(1)))
    return Severity.UNKNOWN


# ── Verdict des checks ──────────────────────────────────────────────────────

# La machinerie de livraison juge le PROCESSUS, pas le code livre. La compter
# reviendrait a bloquer une PR parce qu'une colonne manque quelque part.
#
# Cette liste est un DEFAUT, pas une verite : les noms ci-dessous sont ceux
# d'une organisation donnee, et un autre projet a les siens. Le profil peut la
# remplacer (`ignored_checks`) — un moteur agnostique n'a pas a connaitre les
# jobs de qui que ce soit.
#
# Les motifs sont volontairement ANCRES quand le nom est un mot courant
# (`release`), et libres quand le job apparait aussi sous la forme
# « <workflow> / <job> » (`dependency-gate / check-dependencies`).
DEFAULT_IGNORED_CHECKS: tuple[str, ...] = (
    r"quality[- ]gate",
    r"project[- ]sync",
    r"dependency[- ]gate",
    r"branch[- ]policy",
    r"^hotfix[- ]backmerge",
    r"^release$",
    r"^claude$",
)

_DEFAUT = re.compile("|".join(DEFAULT_IGNORED_CHECKS), re.IGNORECASE)


def compile_ignored(motifs: list[str] | tuple[str, ...] | None) -> re.Pattern[str]:
    """Compile les motifs de checks a ignorer. `None` -> le defaut documente."""
    if not motifs:
        return _DEFAUT
    return re.compile("|".join(motifs), re.IGNORECASE)

# Conclusions qui disent « ce code va bien ».
_CONCLUANTES = frozenset({"success", "neutral", "skipped"})

# Conclusions qui ne disent RIEN — ni bon, ni mauvais.
#   `cancelled` : un run annule n'a pas echoue, il n'a pas fini. `cancel-in-
#                 progress` annule des qu'un push rend un run caduc, et c'est le
#                 cas NORMAL.
#   `stale`     : GitHub abandonne un run dont la file a expire.
#
# Ne PAS y ajouter `timed_out` (le code ne finit pas : c'est un vrai echec) ni
# `action_required` (il faut une intervention).
_MUETTES = frozenset({"cancelled", "stale"})


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str                 # queued | in_progress | completed
    conclusion: str | None      # success | failure | cancelled | ...

    def ignored_by(self, pattern: re.Pattern[str]) -> bool:
        """Ce check juge-t-il le processus plutot que le code livre ?"""
        return bool(pattern.search(self.name))

    @property
    def ours(self) -> bool:
        """Raccourci sur le motif par defaut, pour les appels sans profil."""
        return self.ignored_by(_DEFAUT)

    @property
    def pending(self) -> bool:
        return self.status != "completed"

    @property
    def failed(self) -> bool:
        if self.pending or self.conclusion is None:
            return False
        return self.conclusion not in _CONCLUANTES and self.conclusion not in _MUETTES

    @property
    def mute(self) -> bool:
        return not self.pending and self.conclusion in _MUETTES


@dataclass(frozen=True, slots=True)
class Comment:
    """Un message dans un fil. Le fil entier, pas seulement son ouverture.

    Lire le PREMIER commentaire suffisait a decider s'il y avait du travail ;
    il ne suffit plus a decider s'il en RESTE. Une remarque a laquelle l'agent a
    repondu, une question a laquelle l'humain a repondu, une objection posee
    sous une remarque : les trois ont le meme premier commentaire et trois
    suites differentes.
    """

    id: int
    author: str
    body: str = ""

    @property
    def from_agent(self) -> bool:
        return AGENT_MARK in self.body

    @property
    def asks_human(self) -> bool:
        return ASK_MARK in self.body


@dataclass(frozen=True, slots=True)
class Thread:
    """Un fil de revue, tel que la forge le rend."""

    id: str
    comment_id: int
    author: str
    resolved: bool
    body: str
    path: str | None = None
    line: int | None = None
    # Le fil COMPLET, ouverture comprise. Vide quand l'appelant n'a lu que le
    # premier commentaire : `trail` retombe alors dessus, pour que tout le code
    # ecrit avant cette notion continue de valoir.
    comments: tuple[Comment, ...] = ()

    @property
    def severity(self) -> Severity:
        return severity_of(self.body)

    @property
    def trail(self) -> tuple[Comment, ...]:
        """Les messages du fil, du plus ancien au plus recent. Jamais vide."""
        if self.comments:
            return self.comments
        return (Comment(self.comment_id, self.author, self.body),)

    @property
    def last(self) -> Comment:
        return self.trail[-1]

    @property
    def awaiting_human(self) -> bool:
        """L'agent a-t-il pose une question restee sans reponse ?

        On ne regarde que le DERNIER message. Une question suivie d'une reponse
        n'est plus une attente — c'est precisement ce qui redeclenche le
        travail, sans qu'aucun etat n'ait eu besoin d'etre stocke quelque part.
        """
        return self.last.from_agent and self.last.asks_human

    @property
    def last_seen_id(self) -> int:
        """Identifiant du dernier message LU, quel qu'en soit l'auteur.

        C'est ce qu'on inscrit au curseur une fois le fil traite, et pas
        `cursor_for(trust)` : le curseur dit « j'ai vu jusque-la », pas « j'ai
        approuve jusque-la ». Le filtre de confiance a son role a l'ENTREE, pour
        decider s'il y a du travail ; l'appliquer aussi a la SORTIE ferait qu'un
        profil sans liste de confiance n'avancerait jamais son curseur, donc
        retraiterait les memes remarques indefiniment.
        """
        return max((c.id for c in self.trail), default=self.comment_id)

    def cursor_for(self, trust: frozenset[str]) -> int:
        """Identifiant du dernier message QUI COMPTE, pour le curseur.

        Seuls les messages d'auteurs de confiance sont pris. Deux raisons, et la
        seconde est la vraie : un tiers ne doit pas pouvoir faire travailler
        l'agent en commentant, et son texte ne doit pas entrer dans le prompt
        parce qu'il a fait avancer un compteur.

        Les messages de l'agent sont exclus meme quand son compte est de
        confiance : sa propre reponse ferait avancer le curseur au-dela de la
        remarque qu'elle traite, et la remarque suivante du meme relecteur
        passerait pour deja traitee.
        """
        # On RENORMALISE ici, au lieu d'exiger que l'appelant l'ait fait.
        # `decide` le faisait, les autres appelants non — et l'oubli ne leve
        # rien : une forme non normalisee ne reconnait aucun auteur, donc rend
        # zero, donc « rien de neuf ». Exactement la panne muette que
        # `normalise_login` existe pour empecher. L'operation est idempotente :
        # la refaire ne coute rien.
        confiance = frozenset(normalise_login(r) for r in trust)
        ids = [c.id for c in self.trail
               if not c.from_agent and normalise_login(c.author) in confiance]
        return max(ids, default=0)

    def voices(self, trust: frozenset[str]) -> tuple[Comment, ...]:
        """Messages a montrer a l'agent : l'ouverture, les siens, et ceux de confiance.

        Le texte d'un tiers venu commenter sous une remarque n'entre PAS dans le
        prompt. Le cadrage de `prompt.py` reduit la surface d'injection, il ne la
        supprime pas — et rien n'oblige a l'exposer a quelqu'un qui n'a aucun
        role dans la revue.

        L'OUVERTURE est toujours gardee, quelle que soit la liste. C'est elle,
        la remarque a traiter : la filtrer reviendrait a soumettre a l'agent un
        fil dont on a retire l'objet. Une premiere version le faisait, et une
        liste de confiance vide produisait un prompt sans une seule remarque —
        panne muette, puisqu'un prompt vide se lit comme un fil sans contenu.
        Le tri de confiance qui compte a deja eu lieu en amont, dans `_fresh` :
        ce fil n'est ici que parce qu'un auteur de confiance y a parle.
        """
        confiance = frozenset(normalise_login(r) for r in trust)
        garde = [self.trail[0]]
        for c in self.trail[1:]:
            if c.from_agent or normalise_login(c.author) in confiance:
                garde.append(c)
        return tuple(garde)


@dataclass(frozen=True, slots=True)
class PullSnapshot:
    """Tout ce qu'il faut savoir d'une PR pour trancher. Rien de plus.

    `checks_concluded_at` est le moment ou le dernier check a rendu son verdict.
    C'est de la que part la fenetre de revue : compter depuis l'ouverture de la
    PR ferait expirer la fenetre avant meme que la CI ait fini sur les gros
    depots.
    """

    number: int
    repo: str
    head_sha: str
    # Nom de la branche de tete. Le worktree se monte dessus : sans lui, on ne
    # saurait pas sur quoi travailler, seulement quel commit regarder.
    head_ref: str = ""
    # Qui a ouvert la PR. Le moteur ne s'en sert PAS pour decider — c'est le
    # balayage qui l'emploie, pour dire quelles PR ce demon prend en charge.
    # Le distinguer compte : une regle du moteur vaut pour tout le monde, un
    # perimetre appartient a une installation.
    author: str = ""
    # Branche VISEE. Elle etait lue par la requete GraphQL et jetee aussitot :
    # le demon savait d'ou venait une PR, jamais ou elle allait. Il ne pouvait
    # donc pas distinguer une PR de livraison (`dev` -> `main`) d'une PR de
    # travail, alors que les deux ne se traitent pas pareil.
    base_ref: str = ""
    draft: bool = False
    merged: bool = False
    closed: bool = False
    checks: tuple[Check, ...] = ()
    threads: tuple[Thread, ...] = ()
    checks_concluded_at: datetime | None = None
    # Faux quand la CI n'a PAS PU etre lue — jeton sans droit, API en panne.
    # Sans ce drapeau, `checks=()` est ambigu : il dit aussi bien « aucun check
    # sur cette PR » que « je n'ai pas reussi a les lire », et les deux se
    # lisent alors comme « rien de rouge, rien en attente », donc comme un feu
    # vert. Une PR dont la CI est rouge passerait pour bonne.
    checks_readable: bool = True

    # Etat que le demon tient lui-meme (bail local + commentaire epingle).
    lease_held: bool = False
    review_cycle: int = 0
    last_handled_comment_id: int = 0
    nudge_sent: bool = False
    # SHA pour lequel l'humain a deja ete prevenu. Vient de l'etat local, pas
    # de la forge. Sans lui, `decide` redemandait un arbitrage a chaque
    # passage : le job partait, arrivait au bout, constatait que la question
    # etait deja posee, et sortait — toutes les cinq minutes.
    human_asked_sha: str = ""

    def relevant_checks(self, ignored: re.Pattern[str] | None = None) -> tuple[Check, ...]:
        motif = ignored or _DEFAUT
        return tuple(c for c in self.checks if not c.ignored_by(motif))


@dataclass(frozen=True, slots=True)
class Decision:
    state: State
    reason: str                                   # phrase lisible, ecrite ICI
    actions: tuple[Action, ...] = ()
    threads: tuple[Thread, ...] = field(default_factory=tuple)
    consumes_cycle: bool = False

    @property
    def why(self) -> str:
        return self.reason


def normalise_login(login: str) -> str:
    """Forme comparable d'un identifiant d'auteur.

    GitHub rend le MEME bot sous deux noms selon l'API interrogee :

        REST     "chatgpt-codex-connector[bot]"
        GraphQL  "chatgpt-codex-connector"

    Une allowlist ecrite dans l'une des deux formes ne reconnait donc rien de ce
    que rend l'autre — et la panne est muette : zero fil retenu se lit
    exactement comme zero fil ouvert. L'agent conclurait « rien a faire » pour
    toujours.

    On compare donc sur la forme SANS suffixe, des deux cotes. C'est la seule
    solution qui survive a un profil ecrit dans l'une ou l'autre convention.
    """
    l = (login or "").strip().lower()
    return l[:-5] if l.endswith("[bot]") else l


def _fresh(threads: tuple[Thread, ...], trust: frozenset[str], cursor: int,
           *, malgre_attente: bool = False) -> tuple[Thread, ...]:
    """Fils qui constituent du travail NON TRAITE.

    Quatre conditions, et chacune ecarte un piege reel :

      - `not resolved` : un fil resolu est solde, meme si la remarque reste
        visible ;
      - `not awaiting_human` : l'agent a pose une question et personne n'a
        repondu. Ce fil n'est pas du travail, c'est une attente — le reprendre
        reposerait la meme question a chaque passage. SAUF si on a demande a
        reprendre : c'est precisement le sens du geste, « arrete d'attendre ma
        reponse et refais un tour ». Sans cette exception, un forcage retirait
        le fil de l'attente sans le rendre au travail, et la PR tombait sur
        « rien a faire » — donc « prete a merger », un fil ouvert au nez ;
      - un auteur de confiance a parle : l'allowlist vient du profil, JAMAIS de
        la charge utile. C'est ce qui empeche un commentaire arbitraire de faire
        travailler l'agent. La comparaison passe par `normalise_login`, sans quoi
        la forme GraphQL d'un bot ne reconnait pas la forme REST du meme bot ;
      - `cursor_for(trust) > cursor` : le curseur porte sur l'IDENTIFIANT, jamais
        sur la date ni sur le SHA. Une remarque re-ancree sur un nouveau commit
        voit son `commit_id` et sa ligne changer, mais garde son `id` et son
        `created_at` — la dater ou la situer la ferait retraiter a chaque push.

    C'est la troisieme condition qui porte la RELANCE. Elle regarde tout le fil,
    pas son ouverture : quand l'humain repond a une question de l'agent, son
    message est un identifiant de plus, superieur au curseur, signe d'un auteur
    de confiance — donc du travail. Rien n'a eu besoin d'etre stocke ni d'un
    evenement particulier : l'etat present suffit, comme partout ailleurs ici.
    """
    return tuple(
        t for t in threads
        if not t.resolved
        and (malgre_attente or not t.awaiting_human)
        and t.cursor_for(trust) > cursor
    )


def decide(
    pr: PullSnapshot,
    *,
    trusted_reviewers: frozenset[str] | set[str] | list[str] = frozenset(),
    max_review_cycles: int = 3,
    review_window_s: float = 600.0,
    nudge_enabled: bool = True,
    ignored_checks: re.Pattern[str] | None = None,
    forced: bool = False,
    now: datetime | None = None,
) -> Decision:
    """Que faut-il faire de cette PR, maintenant ?

    `now` est un PARAMETRE : une regle qui lit l'horloge elle-meme n'est pas
    testable, et se met a dependre du moment ou la suite tourne.
    """
    now = now or datetime.now(timezone.utc)
    trust = frozenset(normalise_login(r) for r in trusted_reviewers)

    # ── Cas ou il n'y a rien a faire, quoi qu'il arrive ─────────────────────
    if pr.merged or pr.closed:
        return Decision(State.IDLE, "PR fermee : plus rien a faire.")
    if pr.draft:
        # Un brouillon dit « commence, pas pret a relire ». S'en saisir
        # reviendrait a travailler par-dessus quelqu'un.
        return Decision(State.IDLE, "PR en brouillon : le travail est en cours cote humain.")

    # ── Le seul etat reellement stocke ─────────────────────────────────────
    if pr.lease_held:
        return Decision(State.AGENT_WORKING, f"Un job tient deja le bail sur {pr.repo}#{pr.number}.")

    checks = pr.relevant_checks(ignored_checks)
    failed = [c for c in checks if c.failed]
    pending = [c for c in checks if c.pending]
    frais = _fresh(pr.threads, trust, pr.last_handled_comment_id,
                   malgre_attente=forced)
    # ── LE FORCAGE ─────────────────────────────────────────────────────────
    #
    # Deux verrous arretent une PR en attendant UNE PERSONNE : les cycles
    # epuises (« la boucle ne converge pas »), et une question posee sans
    # reponse. Les deux sont justes — et les deux n'ont aucune sortie autre
    # qu'un humain. Quand cette personne dit « vas-y quand meme », il faut que
    # ce soit possible sans editer un YAML ni une base.
    #
    # Un forcage leve CES DEUX-LA, et rien d'autre. Il ne touche pas au bail
    # (deux agents sur une PR restent exclus), ni aux branches partagees, ni
    # aux verifications avant commit, ni au plafond de jobs du jour — celui-la
    # borne le cout, il s'eleve dans le profil, pas en cliquant.
    #
    # Il est legitime parce que le declencheur n'est plus un commentaire lu sur
    # la forge, mais une personne devant sa console.
    cycles_epuises = pr.review_cycle >= max_review_cycles and not forced

    # L'humain a-t-il DEJA ete prevenu pour cette tete ? `prevenir` porte ce
    # garde depuis toujours, mais tout au bout de la chaine : la decision
    # redemandait un arbitrage a chaque passage, le filtre de `_run` laissait
    # passer, un job partait et ne faisait rien. 67 cycles sur mobile#157.
    #
    # Un fait connu qui change ce qu'il y a a faire appartient a la DECISION.
    # La branche « question en attente » n'emet deja que le label, et ne boucle
    # pas ; celle-ci s'aligne. L'etat reste NEEDS_HUMAN — c'est vrai — seule
    # l'action disparait une fois faite.
    def _arret(raison: str, fils=()) -> Decision:
        actes = (Action.LABEL_NEEDS_HUMAN,)
        if not (pr.human_asked_sha and pr.human_asked_sha == pr.head_sha):
            actes += (Action.ASK_HUMAN,)
        return Decision(State.NEEDS_HUMAN, raison, actes, threads=fils)
    # Fils ou l'agent a pose une question et attend. Ils ne sont PAS du travail
    # (cf. `_fresh`), mais ils ne sont pas rien non plus : sans eux, une PR
    # entierement suspendue a un arbitrage se lirait « prete pour l'humain »,
    # c'est-a-dire prete a merger.
    attente = (() if forced else
               tuple(t for t in pr.threads if not t.resolved and t.awaiting_human))

    # ── La CI n'a pas pu etre lue : on s'arrete, on ne devine pas ──────────
    # Place AVANT toute lecture de `failed` / `pending`, qui seraient vides et
    # donc indiscernables d'une CI verte.
    if not pr.checks_readable:
        return Decision(
            State.NEEDS_HUMAN,
            "etat de la CI illisible (droits du jeton ou API indisponible) : "
            "impossible de distinguer une CI verte d'une CI qu'on ne voit pas.",
            (Action.LABEL_NEEDS_HUMAN,),
        )

    # ── Checks rouges : c'est du travail, et il consomme un cycle ──────────
    # Le placer AVANT les fils est deliberé : corriger une remarque sur une
    # branche dont la CI est rouge produit un correctif qu'on ne sait pas
    # valider.
    if failed:
        noms = ", ".join(c.name for c in failed[:3])
        if cycles_epuises:
            return _arret(
                f"{len(failed)} check(s) en echec ({noms}) et {pr.review_cycle} cycle(s) "
                f"deja consommes : la cause n'est probablement pas celle qu'on croit."
            )
        return Decision(
            State.NEEDS_FIX,
            f"{len(failed)} check(s) en echec : {noms}.",
            (Action.RUN_AGENT,),
            consumes_cycle=True,
        )

    # ── Un check n'a pas conclu ────────────────────────────────────────────
    # On attend AVANT de regarder les fils : une remarque corrigee pendant que
    # la CI tourne encore rouvrirait un cycle sur un etat qui va changer.
    if pending:
        noms = ", ".join(c.name for c in pending[:3])
        return Decision(State.WAITING_CI, f"{len(pending)} check(s) en cours : {noms}.")

    # ── Des remarques non traitees ─────────────────────────────────────────
    if frais:
        bloquants = [t for t in frais if t.severity.blocking]
        if not bloquants:
            # Que du mineur : ca ne vaut pas un cycle d'agent, et ouvrir une
            # issue engage le backlog de quelqu'un d'autre. On PROPOSE.
            return Decision(
                State.READY_FOR_HUMAN,
                f"{len(frais)} remarque(s) mineure(s) (P3) et rien de bloquant : "
                "corps d'issue de dette propose, a toi de decider.",
                (Action.LABEL_READY, Action.PROPOSE_DEBT),
                threads=frais,
            )
        if cycles_epuises:
            return _arret(
                f"{len(bloquants)} remarque(s) bloquante(s) apres {pr.review_cycle} cycle(s) : "
                "la boucle ne converge pas.",
                frais,
            )
        ou = ", ".join(
            f"{t.path}:{t.line}" for t in bloquants[:3] if t.path
        ) or "sans ancrage de fichier"
        return Decision(
            State.NEEDS_FIX,
            f"{len(bloquants)} fil(s) ouvert(s) sur {ou}.",
            (Action.RUN_AGENT,),
            threads=frais,
            consumes_cycle=True,
        )

    # ── Une question a ete posee, et personne n'y a repondu ────────────────
    # Place APRES les fils frais : une reponse arrivee entre-temps est du
    # travail, et le travail passe avant l'attente. Place AVANT la fenetre de
    # revue et le verdict final, parce qu'une PR suspendue a un arbitrage n'est
    # ni « en attente de revue » ni « prete a merger » — et la lire comme prete
    # serait la lire comme mergeable.
    if attente:
        ou = ", ".join(f"{t.path}:{t.line}" for t in attente[:3] if t.path)
        return Decision(
            State.NEEDS_HUMAN,
            f"{len(attente)} question(s) posee(s) dans les fils"
            f"{f' ({ou})' if ou else ''}, en attente de reponse. "
            "Repondre dans le fil relance l'agent.",
            (Action.LABEL_NEEDS_HUMAN,),
            threads=attente,
        )

    # ── Checks verts, aucune remarque : reste-t-il une revue a venir ? ─────
    if pr.checks_concluded_at is not None:
        ecoule = (now - pr.checks_concluded_at).total_seconds()
        if ecoule < review_window_s:
            reste = int(review_window_s - ecoule)
            return Decision(
                State.WAITING_REVIEW,
                f"Checks verts ; fenetre de revue ouverte encore {reste} s.",
            )
        if nudge_enabled and not pr.nudge_sent:
            # La revue n'est pas garantie. Une relance, une seule : deux
            # relances sur une PR que personne ne relit, c'est du bruit, pas de
            # l'insistance.
            return Decision(
                State.WAITING_REVIEW,
                f"Aucune revue apres {int(review_window_s)} s : relance du relecteur (une fois).",
                (Action.NUDGE_REVIEW,),
            )

    return Decision(
        State.READY_FOR_HUMAN,
        "Checks verts, aucun fil ouvert : le merge est une decision humaine.",
        (Action.LABEL_READY,),
    )
