"""Configuration du demon : deux fichiers YAML, et rien d'autre.

    runner.yaml            la machine  — chemins, API locale, reveil, Claude
    profils/<projet>.yaml  un projet   — depots, acces, verifications, budget

Copier un profil suffit a ajouter un projet. Le moteur n'est pas touche.

CINQ REGLES, et elles portent le reste du systeme
-------------------------------------------------

1. AUCUN SECRET DANS LE YAML. Les valeurs sensibles sont des REFERENCES
   (`env:PAT_WRITE`), resolues au moment de l'usage. Les fichiers restent
   versionnables et lisibles sans precaution, et une configuration se valide
   sans avoir le moindre secret sous la main.

2. VALIDE AU CHARGEMENT, cle inconnue = ERREUR. `extra="forbid"` partout.
   Un `acces:` mal orthographie doit refuser de demarrer, jamais retomber sur
   un defaut permissif : c'est la difference entre une faute de frappe visible
   et un agent qui ecrit la ou on croyait l'avoir interdit.

3. LES DEFAUTS SONT LES VALEURS SURES. `default_access: context`,
   `writes_enabled: false`. Une cle absente donne toujours l'agent le plus
   inoffensif — meme polarite defensive que le `DRY_RUN` du Worker, ou seule la
   chaine exacte "false" arme les ecritures.

4. UN PROFIL INVALIDE N'EMPORTE PAS LES AUTRES (cf. `load_profiles`). Un
   `runner.yaml` invalide, lui, empeche le demarrage : sans lui on ne sait meme
   pas ou ecrire les journaux.

5. RECHARGEMENT ENTRE DEUX JOBS, JAMAIS PENDANT. Ces fichiers seront edites
   souvent ; un job dont la configuration change en cours de route serait
   irreproductible.
"""

from __future__ import annotations

import os
import re
from datetime import time as _time
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      field_validator, model_validator)

from reviewer.rules.machine import BRANCHES_PARTAGEES, Severity

__all__ = [
    "Access",
    "ConfigError",
    "EFFORTS",
    "HumanConfig",
    "MoteurConfig",
    "ProfileConfig",
    "RepoConfig",
    "RunnerConfig",
    "SecretRef",
    "load_profile",
    "load_profiles",
    "load_runner",
    "parse_duration",
]


class ConfigError(Exception):
    """Configuration illisible, invalide, ou contradictoire."""


# Niveaux d'effort de raisonnement du SDK. Ensemble FERME, contrairement au nom
# de modele : un nom de modele inconnu est refuse par le serveur avec un message
# clair, alors qu'une faute de frappe sur l'effort ne serait rattrapee nulle
# part — le job tournerait a un niveau que personne n'a choisi.
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


def _valider_effort(v: str | None) -> str | None:
    """Regle unique, partagee par le reglage global et ceux par severite.

    Ecrire deux fois la meme validation, c'est se garantir qu'elles divergeront
    — et c'est toujours la moins stricte qui laisse passer la faute de frappe.
    """
    if v is None:
        return None
    v = v.strip().lower()
    if v not in EFFORTS:
        raise ValueError(
            f"effort={v!r} inconnu. Valeurs acceptees : {', '.join(EFFORTS)}."
        )
    return v


def _valider_modele(v: str | None) -> str | None:
    # Le NOM du modele n'est pas valide ici : la liste bouge, et un nom inconnu
    # est refuse par le serveur avec un message clair. On refuse seulement la
    # chaine vide, qui passerait pour un choix alors qu'elle retomberait sur le
    # defaut du CLI sans le dire.
    if v is None:
        return None
    if not v.strip():
        raise ValueError(
            "model: '' n'est pas un choix — laisser la cle absente pour "
            "prendre le defaut du CLI, ou nommer un modele."
        )
    return v.strip()


# ── Types de champ ──────────────────────────────────────────────────────────

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|m|h)\s*$", re.IGNORECASE)
_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(value: str | int | float) -> float:
    """« 50s », « 5m », « 2h » -> secondes.

    Un nombre nu est REFUSE. `poll_wait: 50` ne dit pas s'il s'agit de secondes
    ou de minutes, et les deux lectures sont plausibles : autant l'ecrire.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        raise ValueError(
            f"duree sans unite : {value!r}. Ecrire « {value}s », « {value}m » ou « {value}h »."
        )
    m = _DURATION_RE.match(str(value))
    if not m:
        raise ValueError(f"duree illisible : {value!r}. Formes acceptees : « 30s », « 5m », « 2h ».")
    return float(m.group(1)) * _UNITS[m.group(2).lower()]


Duration = Annotated[float, Field(description="duree en secondes, ecrite « 30s » / « 5m » / « 2h »")]


class SecretRef(str):
    """Reference vers un secret, jamais le secret lui-meme.

    Deux formes, et une seule interdite : la valeur en clair.

        env:NOM_DE_VARIABLE          lu dans l'environnement
        keyring:SERVICE/COMPTE       lu dans le trousseau du systeme

    Le refus du clair est le seul moyen fiable d'empecher un secret de finir
    dans un fichier versionne : personne ne relit un YAML pour ca.

    ── POURQUOI PAS UN FICHIER CHIFFRE ─────────────────────────────────────

    C'est la demande naturelle, et elle ne tient pas. Un demon qui redemarre
    tout seul doit pouvoir dechiffrer tout seul : la cle doit donc etre a sa
    portee, sur la meme machine, souvent dans le meme dossier. Chiffrer avec une
    cle posee a cote du chiffre, ce n'est pas du chiffrement, c'est de
    l'obfuscation — et une obfuscation qu'on prend pour du chiffrement est pire
    que rien, parce qu'on cesse de se mefier.

    Le TROUSSEAU, lui, protege vraiment : la cle est detenue par le systeme,
    liberee par la session de l'utilisateur, et jamais posee sur le disque du
    projet. C'est ce que fait `keyring:`.

    En conteneur, il n'y a pas de trousseau : la forme qui convient est `env:`,
    alimentee par les secrets de l'orchestrateur. Le message d'erreur le dit,
    plutot que de laisser chercher.

    La resolution est PARESSEUSE (`resolve()`), pas faite au chargement : on
    veut pouvoir valider une configuration sur une machine qui n'a aucun des
    secrets — en CI, ou en relisant le profil d'un autre poste.
    """

    __slots__ = ()

    PREFIXES = ("env:", "keyring:")

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler):  # noqa: D105
        from pydantic_core import core_schema

        return core_schema.no_info_after_validator_function(
            cls._valider, core_schema.str_schema()
        )

    @staticmethod
    def _valider(v: str) -> "SecretRef":
        if not any(v.startswith(p) for p in SecretRef.PREFIXES):
            raise ValueError(
                "un secret s'ecrit « env:NOM_DE_VARIABLE » ou "
                "« keyring:SERVICE/COMPTE », jamais en clair dans le YAML"
            )
        corps = v.split(":", 1)[1].strip()
        if not corps:
            raise ValueError(f"« {v.split(':', 1)[0]}: » sans nom")
        if v.startswith("keyring:") and "/" not in corps:
            raise ValueError(
                "une reference de trousseau s'ecrit « keyring:SERVICE/COMPTE » "
                f"— il manque le compte dans « {v} »"
            )
        return SecretRef(v)

    @property
    def source(self) -> str:
        """« env » ou « keyring »."""
        return self.split(":", 1)[0]

    @property
    def var(self) -> str:
        """Le corps de la reference : le nom de variable, ou SERVICE/COMPTE."""
        return self.split(":", 1)[1].strip()

    def resolve(self) -> str:
        """Lit le secret. Leve si absent ou vide.

        Absent et vide sont traites pareil, volontairement : une valeur posee a
        la chaine vide est une erreur de lancement, pas une intention.
        """
        if self.source == "keyring":
            return self._du_trousseau()
        val = os.environ.get(self.var, "")
        if not val:
            raise ConfigError(
                f"la variable d'environnement {self.var} est absente ou vide "
                f"(referencee par « {self} »)"
            )
        return val

    def _du_trousseau(self) -> str:
        service, _, compte = self.var.partition("/")
        try:
            import keyring  # noqa: PLC0415 — dependance optionnelle
        except ImportError as e:
            raise ConfigError(
                f"« {self} » demande le trousseau du systeme, et le paquet "
                "`keyring` n'est pas installe : `pip install keyring`.\n"
                "En conteneur il n'y a pas de trousseau — employer « env:NOM » "
                "et alimenter la variable par les secrets de l'orchestrateur."
            ) from e
        try:
            val = keyring.get_password(service, compte)
        except Exception as e:  # noqa: BLE001 — les dorsales levent large
            raise ConfigError(
                f"le trousseau n'a pas repondu pour « {self} » : {e}.\n"
                "Sur une machine sans session graphique — un conteneur, un "
                "service systeme — il n'y a pas de dorsale : employer « env:NOM »."
            ) from e
        if not val:
            raise ConfigError(
                f"aucun secret dans le trousseau pour « {self} ». "
                f"Le poser : `reviewer init`, ou en Python "
                f"`keyring.set_password({service!r}, {compte!r}, ...)`."
            )
        return val


class Access(str, Enum):
    """Ce que l'agent a le droit de faire d'un depot.

    L'ordre compte : `context` est le DEFAUT. Un depot qu'on oublie de nommer
    n'est pas ecrivable — on inverse la charge de la preuve, pour qu'un oubli
    de configuration produise un agent inoffensif plutot qu'un agent qui ecrit
    la ou personne ne l'a decide.
    """

    WRITE = "write"      # worktree dedie, jeton d'ecriture, push et PR
    CONTEXT = "context"  # lu seulement : code, conventions, CLAUDE.md, skills
    IGNORE = "ignore"    # n'existe pas pour l'agent


# ── runner.yaml ─────────────────────────────────────────────────────────────


class Strict(BaseModel):
    """Base commune : toute cle inconnue est une erreur (regle 2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


_BOUCLE_LOCALE = ("127.0.0.1", "::1", "localhost")


class ApiConfig(Strict):
    """API locale — c'est l'unique dependance du front de visualisation."""

    bind: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)

    # La derogation, et elle porte son nom. `0.0.0.0` exposerait l'etat des jobs
    # a tout le reseau de la machine ; le demon a ete concu pour n'ouvrir AUCUN
    # port entrant.
    #
    # EN CONTENEUR, la frontiere n'est plus la boucle locale mais le NAMESPACE
    # RESEAU : `127.0.0.1` a l'interieur n'est joignable par personne, meme avec
    # une publication de port. Ecouter sur `0.0.0.0` et publier cote hote en
    # `127.0.0.1:8788:8788` donne exactement la meme surface qu'une ecoute
    # locale sur la machine — pas une de plus.
    #
    # C'est le SEUL cas ou la derogation se justifie, et elle reste explicite :
    # une absence laisse la position sure, comme pour `writes_enabled`.
    reseau_confine: bool = False

    @model_validator(mode="after")
    def _local_sauf_declaration(self) -> "ApiConfig":
        if self.bind in _BOUCLE_LOCALE:
            return self
        if not self.reseau_confine:
            raise ValueError(
                f"bind={self.bind!r} : l'API locale n'ecoute que sur la boucle "
                "locale. Le demon n'expose aucun port entrant.\n"
                "En conteneur, ou la frontiere est le namespace reseau, ajouter "
                "« reseau_confine: true » et publier cote hote sur "
                "127.0.0.1 uniquement."
            )
        return self


class WakeConfig(Strict):
    """Reveil par long-poll sortant.

    `reconcile_every` n'est pas une redondance du reveil : c'est le REPLI. Si le
    Worker tombe, ou si une livraison webhook se perd (GitHub ne les rejoue
    pas), la boucle de reconciliation retrouve le travail toute seule. On y perd
    de la latence, jamais du travail.
    """

    url: str | None = None
    token: SecretRef | None = None
    poll_wait: Duration = 50.0
    reconcile_every: Duration = 300.0

    @field_validator("poll_wait", "reconcile_every", mode="before")
    @classmethod
    def _duree(cls, v: Any) -> float:
        return parse_duration(v)


class ClaudeConfig(Strict):
    """Ce qui pilote le SDK.

    `scrub_env` n'est pas cosmetique. L'ordre de precedence des identifiants
    place `ANTHROPIC_API_KEY` AVANT `CLAUDE_CODE_OAUTH_TOKEN` : une variable qui
    traine dans l'environnement du demon bascule silencieusement la facturation
    de l'abonnement vers l'API. On l'efface, on ne parie pas sur son absence.
    """

    config_dir: Path | None = None
    oauth_token: SecretRef | None = None
    api_key: SecretRef | None = None
    # Marquer comme fiables, DANS le repertoire de configuration du demon, les
    # depots que le profil declare en `access: write`.
    #
    # Sans cela, un espace de configuration neuf n'a accepte le dialogue de
    # confiance nulle part, et Claude Code refuse d'appliquer ce que ces depots
    # declarent dans leur `.claude/settings.json` — leurs HOOKS compris. Le
    # depot perd ses garde-fous a l'instant precis ou un automate ecrit dedans,
    # et il le perd en silence : un avertissement de plus sur une sortie qui en
    # affiche a chaque passage.
    #
    # Le defaut est `true` et ce n'est PAS un elargissement de droits : seuls
    # les depots deja declares en ecriture sont concernes, donc une decision
    # prise en amont, explicitement, depot par depot. Le mettre a `false`
    # redonne le comportement d'avant — utile pour observer, jamais pour
    # produire.
    trust_workspaces: bool = True
    scrub_env: list[str] = Field(
        default_factory=lambda: ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]
    )
    disable_auto_memory: bool = True

    @field_validator("scrub_env")
    @classmethod
    def _garder_la_garde(cls, v: list[str]) -> list[str]:
        # Si l'abonnement est le mode voulu, retirer ANTHROPIC_API_KEY de cette
        # liste est exactement ce qui reintroduit la bascule silencieuse.
        if "ANTHROPIC_API_KEY" not in v:
            raise ValueError(
                "ANTHROPIC_API_KEY doit rester dans scrub_env : elle prime sur "
                "CLAUDE_CODE_OAUTH_TOKEN et ferait facturer l'API au lieu de "
                "l'abonnement, sans rien afficher."
            )
        return v


class RunnerConfig(Strict):
    """Le demon lui-meme. Un seul fichier, jamais copie."""

    worktrees_root: Path
    state_db: Path
    logs_dir: Path
    profiles_dir: Path = Path("./profils")
    api: ApiConfig = Field(default_factory=ApiConfig)
    wake: WakeConfig = Field(default_factory=WakeConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    # Regle 3 : le defaut est la valeur sure. Le lot 1 tourne en lecture seule.
    writes_enabled: bool = False
    # Nombre de jobs menes DE FRONT. Le defaut reste 1 — la valeur qui a servi
    # a observer le demon, et celle dont la sortie se lit sans effort.
    #
    # La borne n'est pas une precaution vague : chaque job fait tourner un agent
    # complet et les tests d'un depot. Trois jobs de front, ce sont trois suites
    # de tests en concurrence sur la meme machine, et trois fois le quota
    # consomme dans la meme minute.
    max_parallel: int = Field(default=1, ge=1, le=8)


# ── profils/<projet>.yaml ───────────────────────────────────────────────────


class ForgeConfig(Strict):
    """La forge, et les DEUX jetons.

    Un PAT fine-grained applique le meme jeu de permissions a TOUS les depots
    qu'il selectionne : on ne peut donc pas panacher « ecriture ici, lecture
    la » dans un seul jeton. D'ou deux jetons, et un refus serveur — pas
    seulement une regle locale — sur les depots en lecture.
    """

    adapter: str = "github"
    org: str
    integration_branch: str = "dev"
    production_branch: str = "main"
    token_write: SecretRef | None = None
    token_read: SecretRef | None = None


class ReviewersConfig(Strict):
    """Qui a le droit de produire du travail, et la relance.

    `trust` est une ALLOWLIST lue depuis ce fichier, jamais depuis la charge
    utile d'un webhook. C'est ce qui empeche un commentaire arbitraire de
    declencher l'agent.

    `nudge` automatise ce qui se fait a la main aujourd'hui : Codex ne passe pas
    sur toutes les PR (mesure : 2 sur 12 sans aucune revue), et attendre un
    evenement qui n'arrivera peut-etre jamais impose une borne de TEMPS, pas
    seulement une borne de cycles.
    """

    trust: list[str] = Field(default_factory=list)
    nudge_comment: str | None = None
    nudge_after: Duration = 600.0
    nudge_once: bool = True

    @field_validator("nudge_after", mode="before")
    @classmethod
    def _duree(cls, v: Any) -> float:
        return parse_duration(v)


class MoteurConfig(Strict):
    """Modele et effort, pour une severite donnee.

    Les deux champs sont FACULTATIFS et independants : ecrire seulement
    `effort: low` sur les P3 garde le modele global. Redire le modele partout
    pour ne changer que l'effort creerait quatre endroits a mettre a jour le
    jour ou il change.
    """

    model: str | None = None
    effort: str | None = None

    # Memes regles que les champs globaux — et litteralement les memes
    # fonctions : deux jeux de validation sur un meme reglage finissent par
    # diverger, et c'est toujours le moins strict qui laisse passer.
    _effort_connu = field_validator("effort")(_valider_effort)
    _modele_non_vide = field_validator("model")(_valider_modele)


class ScopeConfig(Strict):
    """Quelles PR CE demon prend en charge.

    ── POURQUOI CE REGLAGE EXISTE ──────────────────────────────────────────

    Les baux vivent dans une base sqlite LOCALE. Deux demons sur deux machines
    ont deux bases, donc aucune exclusion mutuelle : ils prendraient la meme PR
    en meme temps, pousseraient sur la meme branche, repondraient deux fois dans
    les memes fils, et consommeraient deux fois le quota.

    Le bail ne peut pas resoudre ca sans stockage partage. Ce qui le resout,
    c'est de rendre les ensembles de travail DISJOINTS.

    `authors` vide = TOUTES les PR. C'est le bon defaut pour un demon unique,
    partage par une equipe et tournant sous une identite de service. Des qu'il y
    a plusieurs demons sur les memes depots, il faut le renseigner — sinon ils se
    marchent dessus, et le symptome (deux reponses identiques dans un fil) ne
    designe pas la cause.
    """

    authors: list[str] = Field(default_factory=list)


class HumanConfig(Strict):
    """A qui l'agent s'adresse quand il ne peut pas trancher, et comment.

    `notify` est le seul reglage sans lequel la boucle humaine ne boucle pas :
    une question posee dans un fil que personne ne suit n'est pas une question,
    c'est un fil de plus. On y met la mention qui declenche une notification
    GitHub — « @quelquun ».

    Les deux etiquettes sont FACULTATIVES et sans defaut. Poser un nom par
    defaut ferait echouer chaque pose d'etiquette sur un depot qui ne l'a pas
    creee (GitHub rend 422), et un echec a chaque passage sur un geste
    cosmetique masquerait les vraies pannes. Absente = on n'etiquette pas.
    """

    notify: str | None = None
    label_needs_human: str | None = None
    label_ready: str | None = None

    @field_validator("notify")
    @classmethod
    def _une_mention(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v.startswith("@") or len(v) < 2:
            raise ValueError(
                f"notify={v!r} : ecrire la mention telle qu'elle declenche une "
                "notification GitHub, « @identifiant ». Sans l'arobase, le texte "
                "est publie sans que personne ne soit prevenu — ce qui est "
                "exactement la panne que ce reglage doit empecher."
            )
        return v


class IssuesConfig(Strict):
    """Rattacher le travail a une issue, quand le depot l'exige.

    Beaucoup de depots imposent « toute modification rattachee a une issue ».
    Quand l'agent derive un correctif d'une PR d'integration, ce correctif est
    du travail NEUF : il n'herite d'aucune issue existante, puisque les
    remarques portent sur du code deja fusionne. Il lui en faut donc une.

    `enabled` vaut `false` par defaut (regle 3) : creer une issue engage le
    suivi de quelqu'un d'autre, et un moteur agnostique n'a pas a le decider a
    la place du projet.

    ── POURQUOI L'ID DE CHAMP EST DANS LA CONFIGURATION ────────────────────

    `priority_field` est un identifiant numerique ecrit en clair, ce qui a l'air
    d'une valeur en dur mal placee. Ce n'en est pas une : le catalogue des
    champs d'issue vit sur l'ORGANISATION (`orgs/<org>/issue-fields`), et un PAT
    fine-grained y recoit **403** — mesure du 27/08/2026 sur `PAT_WRITE`. Le
    demon ne peut donc pas le decouvrir ; il faut le lui dire.

    Absent = on ne pose pas de priorite. C'est le defaut sur, et il n'empeche
    rien : l'issue existe, elle est liee, et le champ se remplit a la main.
    """

    enabled: bool = False
    type: str | None = None            # « Bug », « Task »… nom du type d'issue
    priority_field: int | None = None  # id numerique, stable au niveau org
    # Severite de la remarque -> nom d'option de priorite. `Urgent` n'y figure
    # pas volontairement : sur les depots de Damien, cette valeur ouvre la voie
    # d'urgence (`hotfix/`), et une voie d'urgence qu'un automate peut declarer
    # n'est plus une voie d'urgence.
    priority_by_severity: dict[str, str] = Field(
        default_factory=lambda: {"P1": "High", "P2": "Medium",
                                 "P3": "Low", "UNKNOWN": "Medium"})
    # Qui porter comme assigne. Sur un Project V2 pilote par les evenements,
    # l'assignation est souvent LA transition qui place la carte « en cours » :
    # la laisser vide ne casse rien, mais le tableau ne montrera pas le travail.
    assignee: str | None = None

    @field_validator("priority_by_severity")
    @classmethod
    def _pas_d_urgence_automatique(cls, v: dict[str, str]) -> dict[str, str]:
        for severite, priorite in v.items():
            if priorite.strip().lower() == "urgent":
                raise ValueError(
                    f"priority_by_severity[{severite}] = « {priorite} » : un "
                    "automate ne declare pas une urgence. Cette valeur ouvre "
                    "une voie d'exception (hotfix) que seul un humain doit "
                    "pouvoir emprunter."
                )
        return v


class BudgetConfig(Strict):
    """Bornes de consommation.

    Avec l'abonnement, le risque n'est pas une facture : c'est que le demon
    epuise le quota partage avec l'usage humain. Deux plafonds y suffisent.

    Une FENETRE HORAIRE a existe ici, retiree le 26/08/2026 : elle autorisait
    l'agent de 07:00 a 23:00, donc pile pendant les heures de travail qu'elle
    pretendait proteger, et l'eteignait la nuit — le seul moment ou il n'y a
    aucune concurrence sur le quota, et justement celui ou un demon cense
    absorber l'asynchronie a le plus de valeur. Elle faisait l'inverse de sa
    justification. Si une borne horaire revient un jour, ce sera pour une autre
    raison (« ne pas pousser sans personne pour regarder ») et il faudra la
    penser dans ce sens.
    """

    max_jobs_per_day: int = Field(default=12, ge=1)
    max_minutes_per_job: int = Field(default=30, ge=1)



class RepoConfig(Strict):
    """Un depot du profil."""

    access: Access = Access.CONTEXT  # regle 3
    path: Path
    branches: list[str] = Field(default_factory=lambda: ["feat/*", "fix/*", "chore/*", "hotfix/*"])
    checks: list[str] = Field(default_factory=list)
    # Etiquettes posees sur les issues creees pour CE depot — sa « zone », au
    # sens ou beaucoup de projets l'entendent. Vide par defaut : une etiquette
    # qui n'existe pas sur le depot fait repondre 422 a GitHub, et un echec a
    # chaque creation pour un geste de rangement masquerait les vraies pannes.
    labels: list[str] = Field(default_factory=list)

    @field_validator("branches")
    @classmethod
    def _jamais_les_branches_partagees(cls, v: list[str]) -> list[str]:
        # `dev` et `main` ne sont pas des branches de travail. Les autoriser ici
        # reviendrait a laisser l'agent commiter sur l'integration ou la
        # production — le worktree ne protege que du repertoire, pas du ref.
        interdites = BRANCHES_PARTAGEES
        for motif in v:
            if motif in interdites:
                raise ValueError(
                    f"branche {motif!r} interdite : l'agent ne travaille jamais "
                    "directement sur une branche d'integration ou de production."
                )
        return v


class ProfileConfig(Strict):
    """Un projet. C'est CE fichier qu'on copie pour en ajouter un."""

    project: str
    workspace: Path
    forge: ForgeConfig
    reviewers: ReviewersConfig = Field(default_factory=ReviewersConfig)
    human: HumanConfig = Field(default_factory=HumanConfig)
    scope: ScopeConfig = Field(default_factory=ScopeConfig)
    issues: IssuesConfig = Field(default_factory=IssuesConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    plugins: list[Path] = Field(default_factory=list)
    setting_sources: list[str] = Field(default_factory=lambda: ["project"])
    # Skills du depot a activer. `all` = toutes celles que le depot expose.
    # `None` n'est PAS « aucune » cote SDK : c'est « pas de configuration », et
    # les defauts du CLI s'appliquent alors. On l'ecrit donc explicitement,
    # parce que « les skills du projet sont chargees » est une propriete qu'on
    # veut lire dans le profil, pas deduire d'une absence.
    skills: list[str] | str | None = "all"
    # Modele et effort de raisonnement du SDK.
    #
    # `None` n'est PAS « pas de modele » : c'est « le defaut du CLI », qui a
    # valu `claude-sonnet-5` le 27/08/2026 sans que rien nulle part ne le dise.
    # Un reglage qui gouverne le cout ET la qualite du travail ne doit pas etre
    # une valeur qu'on decouvre en relisant un transcrit — on l'ecrit, meme pour
    # redire le defaut.
    model: str | None = None
    effort: str | None = None
    # Le moteur peut dependre de la GRAVITE de ce qu'on corrige. Une coquille de
    # nommage (P3) et un defaut de correction (P1) coutaient jusqu'ici le meme
    # prix, alors que le second merite qu'on y mette les moyens et que le
    # premier ne les justifie pas.
    #
    # Cles acceptees : P1, P2, P3, UNKNOWN. Une cle absente, ou un champ absent
    # dans une entree, retombe sur le reglage global ci-dessus — on ne redit
    # donc que ce qui change.
    per_severity: dict[str, "MoteurConfig"] = Field(default_factory=dict)
    permission_mode: str = "acceptEdits"
    max_review_cycles: int = Field(default=3, ge=1)
    max_turns: int = Field(default=60, ge=1)
    default_access: Access = Access.CONTEXT
    # Checks qui jugent le PROCESSUS et non le code livre : les compter
    # bloquerait une PR parce qu'une colonne manque quelque part. Vide = le
    # defaut documente du moteur (`DEFAULT_IGNORED_CHECKS`) — les noms d'une
    # organisation donnee, qui n'ont rien a faire en dur dans un moteur
    # agnostique.
    ignored_checks: list[str] = Field(default_factory=list)
    # Branche de travail quand la PR relue a pour TETE une branche partagee —
    # le cas d'une PR d'integration ou de release. On ne commite pas sur `dev` :
    # on derive, et le correctif revient par une PR distincte qui vise `dev`.
    # C'est le chemin normal du depot, pas un contournement.
    derived_branch: str = "fix/pr{pr}-revue"
    repos: dict[str, RepoConfig] = Field(default_factory=dict)

    _effort_connu = field_validator("effort")(_valider_effort)
    _modele_non_vide = field_validator("model")(_valider_modele)

    @field_validator("per_severity")
    @classmethod
    def _severites_connues(cls, v: dict[str, "MoteurConfig"]) -> dict[str, "MoteurConfig"]:
        # Une severite mal orthographiee ne doit pas etre ignoree en silence :
        # le job tournerait au reglage global sans que rien ne dise pourquoi
        # celui qu'on croyait avoir pose ne s'applique jamais.
        connues = {s.name for s in Severity}
        if inconnues := set(v) - connues:
            raise ValueError(
                f"severite(s) inconnue(s) : {', '.join(sorted(inconnues))}. "
                f"Acceptees : {', '.join(sorted(connues))}."
            )
        return v

    def moteur(self, severite: "Severity | None") -> tuple[str | None, str | None]:
        """Le couple (modele, effort) a employer pour cette severite.

        Chaque champ retombe INDEPENDAMMENT sur le reglage global : une entree
        qui ne nomme que l'effort garde le modele global. Rendre le couple d'un
        bloc obligerait a redire le modele dans chaque entree, donc a le mettre
        a jour a quatre endroits le jour ou il change.
        """
        reglage = self.per_severity.get(severite.name) if severite else None
        return (
            (reglage.model if reglage else None) or self.model,
            (reglage.effort if reglage else None) or self.effort,
        )

    @field_validator("skills")
    @classmethod
    def _skills(cls, v: list[str] | str | None) -> list[str] | str | None:
        if v is None or isinstance(v, list):
            return v
        if v != "all":
            raise ValueError(
                f"skills={v!r} : seule la chaine « all » est acceptee comme "
                "raccourci. Sinon, ecrire la liste des noms, ou [] pour n'en "
                "activer aucune."
            )
        return v

    @field_validator("derived_branch")
    @classmethod
    def _derivee_lisible(cls, v: str) -> str:
        if "{pr}" not in v and "{issue}" not in v:
            # Sans un numero, deux PR de release se disputeraient la meme
            # branche — et git refuse deux worktrees sur une meme reference.
            # L'erreur arriverait au pire moment : pendant un job.
            raise ValueError(
                f"derived_branch={v!r} doit contenir « {{pr}} » ou « {{issue}} », "
                "sinon deux PR produiraient la meme branche de travail."
            )
        essai = v.replace("{pr}", "0").replace("{issue}", "0")
        if essai in BRANCHES_PARTAGEES:
            raise ValueError(
                f"derived_branch={v!r} designe une branche partagee : c'est "
                "exactement ce que la derivation existe pour eviter."
            )
        return v

    @model_validator(mode="after")
    def _la_branche_ne_promet_pas_ce_qui_n_existera_pas(self) -> "ProfileConfig":
        """`{issue}` dans le nom de branche exige que les issues soient activees.

        La contradiction se paie tard, sinon : le nom de branche ne se calcule
        qu'au moment de monter le worktree, donc en plein job, apres avoir pris
        un bail et consomme un cycle. Une configuration qui ne peut pas
        fonctionner doit refuser de se charger.
        """
        if "{issue}" in self.derived_branch and not self.issues.enabled:
            raise ValueError(
                f"derived_branch={self.derived_branch!r} nomme la branche d'apres "
                "l'issue, mais issues.enabled est faux : aucune issue ne sera "
                "creee, donc ce nom ne pourra jamais etre forme."
            )
        return self

    @field_validator("default_access")
    @classmethod
    def _le_defaut_ne_peut_que_fermer(cls, v: Access) -> Access:
        # Quatrieme refus dur, meme famille que `bypassPermissions` et
        # `bind: 0.0.0.0`. Un defaut a `write` donnerait l'ecriture a tout depot
        # qu'on ajoute SANS y penser : oublier une ligne suffirait a ouvrir un
        # depot. Le reglage ne peut donc que resserrer (`context` -> `ignore`),
        # jamais elargir. Donner l'ecriture reste un geste explicite, depot par
        # depot — c'est ce qui rend la liste `write` lisible comme un inventaire
        # de ce que l'agent peut toucher.
        if v is Access.WRITE:
            raise ValueError(
                "default_access: write est refuse — un defaut ne doit jamais "
                "DONNER l'ecriture. Nommer « access: write » sur chaque depot "
                "concerne, pour que la liste des depots modifiables reste "
                "explicite. Valeurs acceptees : context, ignore."
            )
        return v

    @model_validator(mode="before")
    @classmethod
    def _defaut_dacces(cls, data: Any) -> Any:
        """Applique `default_access` aux depots qui n'ecrivent pas `access:`.

        Sans ceci, `default_access` ne gouvernait RIEN : `RepoConfig.access`
        porte son propre defaut, et le reglage du profil n'etait qu'une seconde
        declaration parallele de la meme idee — lue par personne. Un reglage
        decoratif dans la surface de securite est pire qu'absent : il se lit
        comme une garantie.

        On agit sur le dict BRUT (`mode="before"`) parce que les modeles sont
        `frozen=True` : une fois construits, plus rien n'est modifiable. Et on
        teste la presence de la CLE, pas la valeur — un depot qui ecrit
        `access: context` explicitement doit le rester meme si le profil passe
        a `ignore`.
        """
        if not isinstance(data, dict):
            return data
        defaut = data.get("default_access")
        if defaut is None:
            return data
        depots = data.get("repos")
        if not isinstance(depots, dict):
            return data
        # On ne remplace pas le dict du profil : muter l'entree de l'appelant
        # rendrait un rechargement dependant du precedent.
        data = {**data, "repos": {
            nom: ({**d, "access": defaut}
                  if isinstance(d, dict) and "access" not in d else d)
            for nom, d in depots.items()
        }}
        return data

    @field_validator("permission_mode")
    @classmethod
    def _jamais_bypass(cls, v: str) -> str:
        # `bypassPermissions` IGNORE `allowed_tools` : tout est approuve, y
        # compris Bash. Le seul mode qui borne les ecritures au repertoire de
        # travail est `acceptEdits`. On refuse le mode dangereux ici plutot que
        # d'esperer que personne ne l'ecrive.
        if v == "bypassPermissions":
            raise ValueError(
                "permission_mode=bypassPermissions est refuse : ce mode ignore "
                "allowed_tools et approuve tout, Bash compris. Utiliser "
                "« acceptEdits », qui borne les operations fichier au worktree."
            )
        connus = {"default", "acceptEdits", "plan", "dontAsk", "auto"}
        if v not in connus:
            raise ValueError(f"permission_mode inconnu : {v!r}. Connus : {', '.join(sorted(connus))}")
        return v

    @field_validator("setting_sources")
    @classmethod
    def _sources(cls, v: list[str]) -> list[str]:
        connues = {"user", "project", "local"}
        if inconnues := set(v) - connues:
            raise ValueError(
                f"setting_sources inconnue(s) : {', '.join(sorted(inconnues))}. "
                f"Connues : {', '.join(sorted(connues))}."
            )
        return v

    def model_post_init(self, _ctx: Any) -> None:
        # ── L'echappatoire du conteneur ────────────────────────────────────
        #
        # Un profil est portable partout SAUF sur une ligne : `workspace`, qui
        # dit ou les copies locales vivent sur CETTE machine. En conteneur elles
        # sont sous `/repos`, sur un poste elles sont ailleurs.
        #
        # Dupliquer le profil pour cette seule ligne le ferait deriver, et
        # charger les deux serait pire : `load_profiles` prend TOUS les YAML du
        # dossier, donc deux profils sur les memes depots, donc deux demons qui
        # se marchent dessus.
        #
        # La variable ne vaut que pour `workspace`. Tout le reste du profil —
        # depots, verifications, relecteurs, perimetre — est identique partout.
        if racine := os.environ.get("REVIEWER_WORKSPACE", "").strip():
            object.__setattr__(self, "workspace", Path(racine))

        # `{workspace}` est resolu APRES validation, pour que le fichier reste
        # lisible : un chemin de depot s'ecrit relativement au projet.
        object.__setattr__(self, "workspace", self.workspace.expanduser())
        for repo in self.repos.values():
            brut = str(repo.path)
            if "{workspace}" in brut:
                object.__setattr__(
                    repo, "path", Path(brut.replace("{workspace}", str(self.workspace)))
                )

    @property
    def shared_refs(self) -> frozenset[str]:
        """Branches sur lesquelles le demon ne commite ni ne pousse JAMAIS.

        Tirees du profil, pas d'une constante : un projet dont l'integration
        s'appelle `develop` etait jusqu'ici protege par une liste ecrite pour
        `dev`, donc pas protege du tout. `master` y reste en dur — c'est le
        meme role que `main` sous un autre nom, et l'oublier ne se rattrape pas.
        """
        return frozenset({
            self.forge.integration_branch,
            self.forge.production_branch,
            "master",
        })

    def repos_by_access(self, access: Access) -> dict[str, RepoConfig]:
        return {n: r for n, r in self.repos.items() if r.access is access}

    @property
    def writable(self) -> dict[str, RepoConfig]:
        return self.repos_by_access(Access.WRITE)


# ── Chargement ──────────────────────────────────────────────────────────────


def _lire_yaml(chemin: Path) -> dict[str, Any]:
    try:
        texte = chemin.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"{chemin} illisible : {e}") from e
    try:
        data = yaml.safe_load(texte)
    except yaml.YAMLError as e:
        raise ConfigError(f"{chemin} : YAML invalide — {e}") from e
    if data is None:
        raise ConfigError(f"{chemin} est vide")
    if not isinstance(data, dict):
        raise ConfigError(f"{chemin} : attendu un objet a la racine, trouve {type(data).__name__}")
    return data


def _detailler(chemin: Path, e: ValidationError) -> ConfigError:
    """Message d'erreur lisible : le chemin de la cle, pas une trace pydantic."""
    lignes = []
    for err in e.errors():
        cle = ".".join(str(p) for p in err["loc"]) or "(racine)"
        lignes.append(f"  {cle} : {err['msg']}")
    return ConfigError(f"{chemin} : configuration invalide\n" + "\n".join(lignes))


def load_runner(chemin: Path) -> RunnerConfig:
    """Charge `runner.yaml`. Une erreur ici EMPECHE le demarrage.

    Sans ce fichier on ne sait meme pas ou ecrire les journaux : continuer
    reviendrait a travailler sans laisser de trace.
    """
    try:
        return RunnerConfig.model_validate(_lire_yaml(chemin))
    except ValidationError as e:
        raise _detailler(chemin, e) from e


def load_profile(chemin: Path) -> ProfileConfig:
    """Charge un profil. Leve si invalide — l'appelant decide quoi en faire."""
    try:
        return ProfileConfig.model_validate(_lire_yaml(chemin))
    except ValidationError as e:
        raise _detailler(chemin, e) from e


def load_profiles(dossier: Path) -> tuple[dict[str, ProfileConfig], dict[str, str]]:
    """Charge tous les profils d'un dossier.

    Rend `(profils_valides, erreurs_par_fichier)`. Un profil invalide est
    ECARTE, pas fatal : un projet mal configure ne doit pas arreter ceux qui
    tournent. Mais son erreur est RENDUE, jamais avalee — l'appelant doit la
    journaliser, sinon un profil qui disparait se lit comme un profil qui n'a
    rien a faire.
    """
    profils: dict[str, ProfileConfig] = {}
    erreurs: dict[str, str] = {}
    if not dossier.is_dir():
        raise ConfigError(f"dossier de profils introuvable : {dossier}")
    for f in sorted(dossier.glob("*.yaml")) + sorted(dossier.glob("*.yml")):
        try:
            p = load_profile(f)
        except ConfigError as e:
            erreurs[f.name] = str(e)
            continue
        if p.project in profils:
            erreurs[f.name] = (
                f"le projet « {p.project} » est deja defini par un autre fichier ; "
                "deux profils du meme nom rendraient les journaux ambigus"
            )
            continue
        profils[p.project] = p
    return profils, erreurs
