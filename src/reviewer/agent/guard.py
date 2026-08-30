"""Le garde-fou `PreToolUse` — la seule barriere qui voit TOUS les appels.

L'ordre d'evaluation des permissions du SDK est : hooks -> deny -> ask -> mode
-> allow -> `can_use_tool`. Deux consequences, et elles fondent ce module :

  - un outil auto-approuve (regle `allow`, `acceptEdits`) ne passe JAMAIS par
    `can_use_tool`. Y mettre un controle revient a ne rien controler ;
  - un refus de hook s'applique meme en `bypassPermissions`.

Le hook est donc le seul endroit ou une regle s'applique vraiment a tout.

── POURQUOI LES REGLES `deny` NE SUFFISENT PAS ─────────────────────────────
Une regle `Bash(git push:*)` est un motif de PREFIXE. Elle laisse passer :

    git -C . push            (le drapeau precede le sous-commande)
    cd autre && git push     (la commande n'est pas en tete)
    git push; echo ok        (idem)

Ce module ne fait donc pas de correspondance de prefixe : il DECOUPE la ligne
en sous-commandes (`;`, `&&`, `||`, `|`, saut de ligne), saute les drapeaux, et
juge chaque sous-commande separement.

── CE QU'IL PROTEGE, ET POURQUOI PAS AUTRE CHOSE ───────────────────────────
Il ne cherche pas a etre un bac a sable. Un shell offre trop de chemins pour
qu'une liste de motifs pretende les couvrir tous — c'est pourquoi la garantie
DURE reste ailleurs : les ACL du systeme de fichiers et la portee des jetons.
Ce garde-fou couvre les gestes qu'on peut nommer, ce qui est deja beaucoup :
ecrire hors du worktree, pousser, merger, toucher aux workflows et aux secrets.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePath
from reviewer.rules.machine import BRANCHES_PARTAGEES

__all__ = ["Guard", "Verdict", "PROTECTED_GLOBS"]


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    reason: str = ""

    @property
    def denied(self) -> bool:
        return not self.allowed


ALLOW = Verdict(True)


def deny(raison: str) -> Verdict:
    return Verdict(False, raison)


# Chemins que l'agent ne modifie jamais, meme dans son propre worktree. Le
# rayon d'action y depasse la PR : un workflow modifie s'execute au prochain
# push, un fichier de deploiement touche la production.
PROTECTED_GLOBS: tuple[str, ...] = (
    "**/.github/workflows/**",
    "**/deploy/**",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/id_rsa*",
    "**/.git/config",
    "**/.git/hooks/**",
)

# Sous-commandes git interdites. Ce ne sont pas des « dangers » abstraits :
# chacune a une consequence qu'un correctif de revue n'a aucune raison
# d'exiger, et dont on ne revient pas facilement.
_GIT_INTERDIT = {
    "push":          "le push est fait par le runner, apres verification de la branche et du diff",
    "reset":         "un reset perd du travail non commite sans laisser de trace",
    "clean":         "supprime des fichiers non suivis, y compris ceux qu'on n'a pas ecrits",
    "rebase":        "reecrit l'historique ; l'arbitrage revient a l'humain",
    "filter-branch": "reecrit l'historique entier",
    "filter-repo":   "reecrit l'historique entier",
    "worktree":      "la gestion des worktrees appartient au runner, pas a l'agent",
    "remote":        "changer de remote redirige les pushs suivants",
    "config":        "modifie la configuration du depot, hors du perimetre d'un correctif",
    "submodule":     "hors perimetre",
}

# `gh` : tout ce qui decide du sort d'une PR ou ecrit via l'API.
_GH_INTERDIT = {
    ("pr", "merge"):  "le merge est une decision humaine",
    ("pr", "close"):  "fermer une PR est une decision humaine",
    ("pr", "ready"):  "sortir du brouillon est une decision humaine",
    ("repo", "delete"): "destructif et irreversible",
    ("release", "create"): "publier une release est une decision humaine",
    ("release", "delete"): "destructif",
    ("workflow", "disable"): "modifie la livraison hors du perimetre d'un correctif",
    ("workflow", "enable"): "modifie la livraison hors du perimetre d'un correctif",
    ("secret", "set"): "ecrit un secret",
    ("auth", "logout"): "casserait l'authentification du runner",
}

# Commandes qui reparent l'ENVIRONNEMENT plutot que le code.
#
# Un agent qui lance `pip install`, `npm ci` ou `uv sync` transforme un probleme
# de preparation du job en modification implicite de la machine. C'est le
# runner, l'image Docker ou l'installation du depot qui doivent fournir les
# outils ; si quelque chose manque, l'agent doit s'arreter et le rapporter.
_INSTALLATIONS = {
    "pip": {"install", "uninstall"},
    "pip3": {"install", "uninstall"},
    "pipx": {"install", "uninstall"},
    "npm": {"ci", "i", "install", "uninstall", "update"},
    "pnpm": {"add", "i", "install", "remove", "sync", "update", "up"},
    "yarn": {"add", "install", "remove", "upgrade"},
    "bun": {"add", "install", "remove", "update"},
    "poetry": {"add", "install", "remove", "update"},
    "cargo": {"install"},
    "go": {"get", "install"},
}

# Methodes HTTP d'ecriture pour `gh api`.
_METHODES_ECRITURE = {"POST", "PUT", "PATCH", "DELETE"}

# Reseau -> shell. Le motif est grossier PARCE QUE le cas est grossier : on
# refuse d'executer ce qu'on vient de telecharger, quelle que soit la forme.
_RESEAU_VERS_SHELL = re.compile(
    r"\b(?:curl|wget|iwr|invoke-webrequest|invoke-restmethod)\b[^|;&]*[|]\s*"
    r"(?:sudo\s+)?(?:ba|z|k|fi)?sh\b|\biex\b",
    re.IGNORECASE,
)

# Binaires dont les operandes sont des cibles d'ECRITURE.
#
# On ENUMERE les ecrivains plutot que de refuser tout chemin cite. Refuser
# n'importe quelle commande mentionnant un depot en lecture seule interdirait de
# le LIRE — `cat`, `grep`, `head`, `sed` sans `-i` — alors que c'est exactement
# ce pour quoi il est monte. Un garde-fou qui empeche l'usage prevu se fait
# desactiver.
#
# Ce que cette liste ne couvre pas — `python -c "open(…,'w')"` et ses cousins —
# releve de la garantie DURE : les ACL du systeme de fichiers. Ce module traite
# les gestes qu'on peut nommer, il ne pretend pas etre un bac a sable.
# binaire -> (drapeaux qui consomment la valeur suivante, ou sont les cibles)
#
# La position compte : `cp source destination` n'ecrit que dans le DERNIER
# operande. Traiter `source` comme une cible produirait un refus sur un fichier
# qu'on ne fait que lire — un faux positif, c'est-a-dire la panne qui fait
# desactiver le garde-fou.
#
# `chmod 644 f` et `chown moi f` ont un premier operande qui n'est pas un
# chemin. `truncate -s 0 f` a un `0` qui est la valeur d'un drapeau.
_ECRIVAINS: dict[str, tuple[frozenset[str], str]] = {
    "cp":       (frozenset({"-t", "--target-directory"}), "derniere"),
    "copy":     (frozenset(), "derniere"),
    "mv":       (frozenset({"-t", "--target-directory"}), "derniere"),
    "move":     (frozenset(), "derniere"),
    "ln":       (frozenset({"-t"}), "derniere"),
    "mklink":   (frozenset(), "premiere"),
    "install":  (frozenset({"-m", "-o", "-g", "-t"}), "derniere"),
    "rm":       (frozenset(), "toutes"),
    "del":      (frozenset(), "toutes"),
    "rmdir":    (frozenset(), "toutes"),
    "touch":    (frozenset({"-d", "-r", "-t"}), "toutes"),
    "mkdir":    (frozenset({"-m"}), "toutes"),
    "tee":      (frozenset(), "toutes"),
    "truncate": (frozenset({"-s", "-r", "--size", "--reference"}), "toutes"),
    "chmod":    (frozenset(), "sauf_premiere"),
    "chown":    (frozenset(), "sauf_premiere"),
}

_REDIRECTIONS = {">", ">>", "1>", "2>", "&>"}

_SEPARATEURS = {";", "&&", "||", "|", "&", "\n"}


def _sous_commandes(commande: str) -> list[list[str]]:
    """Decoupe une ligne de shell en sous-commandes tokenisees.

    `posix=False` conserve les antislashs : sous Windows, un chemin est plein
    d'antislashs et les traiter comme des echappements le detruirait.
    """
    lex = shlex.shlex(commande, posix=False, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        jetons = list(lex)
    except ValueError:
        # Guillemet non ferme : on ne sait pas lire la commande. On REFUSE —
        # une commande qu'on ne sait pas analyser n'est pas une commande sure.
        return [["<illisible>"]]

    out: list[list[str]] = [[]]
    for j in jetons:
        if j in _SEPARATEURS:
            out.append([])
        else:
            out[-1].append(j.strip("'\""))
    return [c for c in out if c]


def _operandes_sed(args: list[str]) -> list[str]:
    """Fichiers vises par un `sed`, sans confondre l'expression avec un chemin.

    `sed -i 's/a/b/' fichier` : l'expression contient des `/` et ressemble a un
    chemin relatif. La prendre pour telle refuserait un `sed` parfaitement
    legitime dans le worktree — un garde-fou qui bloque le travail normal finit
    par etre desactive, ce qui est pire que de ne pas l'avoir.

    Regle de sed : sans `-e`/`-f`, le PREMIER operande est le script et le reste
    sont les fichiers. Avec, tous les operandes sont des fichiers.
    """
    script_donne = any(a in ("-e", "-f", "--expression", "--file")
                       or a.startswith(("-e", "-f")) and len(a) > 2
                       for a in args[1:])
    operandes: list[str] = []
    i = 1
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            if a in ("-e", "-f", "--expression", "--file"):
                i += 2      # le script est la valeur du drapeau
                continue
            i += 1
            continue
        operandes.append(a)
        i += 1
    if not script_donne and operandes:
        operandes = operandes[1:]   # le premier operande est le script
    return operandes


def _premier_non_drapeau(args: list[str], depuis: int = 0) -> str | None:
    """Premier argument qui n'est ni un drapeau ni sa valeur.

    C'est ce qui neutralise `git -C <dir> push` : `-C` prend une valeur, et
    sans la sauter on prendrait le repertoire pour le sous-commande.
    """
    i = depuis
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            # Les drapeaux a valeur separee de git/gh les plus courants.
            if a in ("-C", "-c", "--git-dir", "--work-tree", "-R", "--repo"):
                i += 2
                continue
            i += 1
            continue
        return a
    return None


def _nom_exe(brut: str) -> str:
    """Nom comparable d'un executable, sans chemin ni extension."""
    exe = PurePath(brut.strip('"').replace("\\", "/")).name.lower()
    return exe.removesuffix(".exe").removesuffix(".cmd")


def _python(exe: str) -> bool:
    return exe == "py" or bool(re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", exe))


def _normaliser_args(args: list[str]) -> str:
    """Forme stable pour comparer une commande de l'agent a un check du runner."""
    if not args:
        return ""
    exe = _nom_exe(args[0])
    if _python(exe):
        exe = "python"
    return " ".join([exe, *(a.lower() for a in args[1:])])


def _normaliser_commande(commande: str) -> str:
    morceaux = _sous_commandes(commande)
    return _normaliser_args(morceaux[0]) if len(morceaux) == 1 else ""


def _installation_dependances(exe: str, args: list[str]) -> str | None:
    """Cette sous-commande installe-t-elle ou retire-t-elle des dependances ?"""
    if _python(exe):
        for i, a in enumerate(args[1:], 1):
            if a != "-m" or i + 1 >= len(args):
                continue
            module = _nom_exe(args[i + 1])
            if module in ("pip", "pip3"):
                sous = (_premier_non_drapeau(args, i + 2) or "").lower()
                if sous in _INSTALLATIONS["pip"]:
                    return "`python -m pip`"
            if module == "ensurepip":
                return "`python -m ensurepip`"
            return None

    sous = (_premier_non_drapeau(args, 1) or "").lower()
    if sous in _INSTALLATIONS.get(exe, set()):
        return f"`{exe} {sous}`"
    if exe == "uv":
        if sous in {"add", "remove", "sync"}:
            return f"`uv {sous}`"
        if sous == "pip":
            i = args.index(_premier_non_drapeau(args, 1))
            suivant = (_premier_non_drapeau(args, i + 1) or "").lower()
            if suivant in _INSTALLATIONS["pip"]:
                return f"`uv pip {suivant}`"
    return None


class Guard:
    """Juge un appel d'outil. Ne fait AUCUN effet de bord.

    `writable_root` est le worktree du job en cours — le seul endroit ou l'agent
    peut ecrire. `readonly_roots` sont les depots montes en contexte : lisibles,
    jamais modifiables.
    """

    def __init__(
        self,
        *,
        writable_root: Path,
        readonly_roots: tuple[Path, ...] | list[Path] = (),
        protected_globs: tuple[str, ...] = PROTECTED_GLOBS,
        protected_refs: frozenset[str] | set[str] = BRANCHES_PARTAGEES,
        runner_checks: tuple[str, ...] | list[str] = (),
    ) -> None:
        self.writable_root = Path(writable_root).resolve()
        self.readonly_roots = tuple(Path(p).resolve() for p in readonly_roots)
        self.protected_globs = tuple(protected_globs)
        self.protected_refs = frozenset(protected_refs)
        self.runner_checks = frozenset(
            c for c in (_normaliser_commande(cmd) for cmd in runner_checks) if c
        )

    # ── Chemins ────────────────────────────────────────────────────────────

    def _sous(self, chemin: Path, racine: Path) -> bool:
        try:
            chemin.relative_to(racine)
        except ValueError:
            return False
        return True

    def _protege(self, chemin: Path) -> str | None:
        """Ce chemin tombe-t-il sous un motif protege ? Rend le motif."""
        p = PurePath(chemin).as_posix()
        for motif in self.protected_globs:
            if fnmatch(p, motif) or fnmatch(p, motif.lstrip("*/")):
                return motif
        return None

    def check_path_write(self, chemin: str | Path) -> Verdict:
        """Peut-on ECRIRE a ce chemin ?"""
        try:
            resolu = Path(chemin).expanduser().resolve()
        except (OSError, ValueError):
            return deny(f"chemin illisible : {chemin!r}")

        for ro in self.readonly_roots:
            if self._sous(resolu, ro):
                return deny(
                    f"{resolu} est dans un depot monte en LECTURE SEULE ({ro}). "
                    "Si une modification y est necessaire, s'arreter et proposer "
                    "un correctif en diff — ne pas l'appliquer."
                )

        if not self._sous(resolu, self.writable_root):
            return deny(
                f"{resolu} est hors du worktree de ce job ({self.writable_root}). "
                "L'agent n'ecrit que dans son propre arbre."
            )

        if motif := self._protege(resolu):
            return deny(
                f"{resolu} correspond au motif protege « {motif} » : le rayon "
                "d'action de ce fichier depasse la PR."
            )
        return ALLOW

    # ── Commandes ──────────────────────────────────────────────────────────

    def _check_git(self, args: list[str]) -> Verdict:
        sous = _premier_non_drapeau(args, 1)
        if sous is None:
            return ALLOW
        if raison := _GIT_INTERDIT.get(sous):
            return deny(f"`git {sous}` est refuse : {raison}.")
        if sous == "checkout":
            # Basculer sur une branche partagee sortirait du worktree du job.
            for a in args[2:]:
                if a in self.protected_refs:
                    return deny(
                        f"`git checkout {a}` : {a} est une branche partagee, "
                        "l'agent travaille sur sa propre branche."
                    )
        if sous == "branch" and any(a in ("-D", "-d", "--delete") for a in args):
            return deny("`git branch --delete` est refuse : suppression de reference.")
        return ALLOW

    def _check_gh(self, args: list[str]) -> Verdict:
        a1 = _premier_non_drapeau(args, 1)
        if a1 is None:
            return ALLOW
        idx = args.index(a1)
        a2 = _premier_non_drapeau(args, idx + 1)
        if raison := _GH_INTERDIT.get((a1, a2 or "")):
            return deny(f"`gh {a1} {a2}` est refuse : {raison}.")
        if a1 == "api":
            for i, a in enumerate(args):
                if a in ("-X", "--method") and i + 1 < len(args):
                    if args[i + 1].upper() in _METHODES_ECRITURE:
                        return deny(
                            f"`gh api -X {args[i + 1].upper()}` est refuse : les "
                            "ecritures d'API passent par le runner, qui les journalise."
                        )
                if a.startswith("-X") and len(a) > 2 and a[2:].upper() in _METHODES_ECRITURE:
                    return deny(f"`gh api {a}` est refuse : ecriture d'API.")
                if a in ("-f", "-F", "--field", "--raw-field"):
                    # `gh api -f cle=valeur` implique un POST.
                    return deny("`gh api` avec des champs implique une ecriture.")
        return ALLOW

    def check_command(self, commande: str) -> Verdict:
        """Juge une ligne de shell, sous-commande par sous-commande."""
        if _RESEAU_VERS_SHELL.search(commande):
            return deny(
                "executer ce qui vient d'etre telecharge est refuse "
                "(`curl … | sh`, `iwr … | iex`)."
            )

        for args in _sous_commandes(commande):
            if not args:
                continue
            if args[0] == "<illisible>":
                return deny(
                    "commande non analysable (guillemet non ferme) : refusee par "
                    "principe — ce qu'on ne sait pas lire, on ne sait pas juger."
                )
            exe = _nom_exe(args[0])

            if self.runner_checks and _normaliser_args(args) in self.runner_checks:
                return deny(
                    f"`{' '.join(args)}` est une verification officielle du "
                    "profil : rends ton verdict. Le runner la lancera apres, "
                    "avant tout commit, push ou reponse."
                )

            if installation := _installation_dependances(exe, args):
                return deny(
                    f"{installation} est refuse : l'agent ne repare pas "
                    "l'environnement du job. Si un outil manque, s'arreter et "
                    "le rapporter ; les dependances se preparent avant le "
                    "lancement."
                )

            if exe == "git":
                if (v := self._check_git(args)).denied:
                    return v
            elif exe == "gh":
                if (v := self._check_gh(args)).denied:
                    return v
            # Cibles d'ecriture : les operandes d'un ecrivain connu, plus tout
            # ce qui suit une redirection.
            for cible in self._cibles_ecriture(exe, args):
                if (v := self.check_path_write(cible)).denied:
                    return v
        return ALLOW

    def _cibles_ecriture(self, exe: str, args: list[str]) -> list[str]:
        """Chemins que cette sous-commande va ECRIRE."""
        cibles: list[str] = []

        if exe == "sed" and any(a == "-i" or a.startswith("-i") for a in args):
            cibles += _operandes_sed(args)
        elif exe == "dd":
            # `dd if=source of=destination` : la cible est nommee, pas positionnelle.
            cibles += [a[3:] for a in args[1:] if a.startswith("of=")]
        elif exe in _ECRIVAINS:
            drapeaux_a_valeur, position = _ECRIVAINS[exe]
            operandes: list[str] = []
            i = 1
            while i < len(args):
                a = args[i]
                if a.startswith("-"):
                    if a in drapeaux_a_valeur:
                        # `cp -t <dossier> …` : la cible EST la valeur du drapeau,
                        # et tous les operandes deviennent des sources.
                        if a in ("-t", "--target-directory") and i + 1 < len(args):
                            cibles.append(args[i + 1])
                            position = "aucune"
                        i += 2
                        continue
                    i += 1
                    continue
                operandes.append(a)
                i += 1

            if position == "toutes":
                cibles += operandes
            elif position == "sauf_premiere":
                cibles += operandes[1:]
            elif position == "derniere" and operandes:
                cibles.append(operandes[-1])
            elif position == "premiere" and operandes:
                cibles.append(operandes[0])

        # Redirection : la cible est le jeton suivant. `punctuation_chars` isole
        # `>` en jeton propre, mais `> fichier` colle aussi sous la forme
        # `>fichier` selon l'ecriture.
        for i, a in enumerate(args):
            if a in _REDIRECTIONS and i + 1 < len(args):
                cibles.append(args[i + 1])
            elif a.startswith(">") and len(a) > 1 and not a.startswith(">>"):
                cibles.append(a.lstrip(">"))
            elif a.startswith(">>") and len(a) > 2:
                cibles.append(a.lstrip(">"))

        return [c.strip("'\"") for c in cibles if c and not c.startswith("-")]

    # ── Point d'entree ─────────────────────────────────────────────────────

    #: Outils dont l'entree porte un chemin de fichier a ecrire.
    ECRITURE_FICHIER = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})

    def check(self, tool_name: str, tool_input: dict) -> Verdict:
        """Juge un appel. C'est ce que le hook `PreToolUse` appelle."""
        if tool_name in self.ECRITURE_FICHIER:
            chemin = tool_input.get("file_path") or tool_input.get("notebook_path")
            if not chemin:
                return deny(f"{tool_name} sans chemin de fichier : appel non analysable.")
            return self.check_path_write(chemin)

        if tool_name in ("Bash", "PowerShell"):
            commande = tool_input.get("command") or ""
            if not commande.strip():
                return ALLOW
            return self.check_command(commande)

        # Lecture, recherche, navigation : rien a juger ici. La portee de
        # lecture est fixee par `cwd` et `add_dirs`, pas par ce garde-fou.
        return ALLOW

    def as_hook(self):
        """Rend un callback `PreToolUse` au format attendu par le SDK.

        Retour `{}` = laisser passer. Pour bloquer, un `hookSpecificOutput`
        avec `permissionDecision: "deny"` — la raison est renvoyee a Claude
        comme resultat d'outil, donc elle doit lui dire quoi faire a la place.
        """

        async def hook(input_data, tool_use_id, context):  # noqa: ANN001
            v = self.check(input_data.get("tool_name", ""), input_data.get("tool_input", {}))
            if v.allowed:
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": v.reason,
                }
            }

        return hook
