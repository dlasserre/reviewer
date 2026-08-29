"""L'assistant d'installation : `agent-runner-lg init`.

Il pose les questions, verifie ce qu'on lui repond, et ECRIT les deux fichiers de
configuration. Rien d'autre — il ne lance aucun agent et ne touche a aucun depot.

── QUATRE REGLES, ET CHACUNE VIENT D'UNE FACON DE SE TROMPER ───────────────

1. ON VERIFIE LE JETON AVANT D'ECRIRE QUOI QUE CE SOIT. Une configuration qui
   reference un jeton mort est pire que pas de configuration : elle a l'air
   juste, et l'echec arrive plus tard, dans un message qui parle d'autre chose.
   L'assistant appelle la forge, lit le compte et les droits, et le DIT.

2. ON N'ECRASE JAMAIS EN SILENCE. Un fichier existant est sauvegarde a cote
   avant d'etre remplace, et le chemin de la sauvegarde est affiche.

3. LES SECRETS NE SONT PAS ECRITS DANS LE YAML. Ils vont dans le trousseau du
   systeme, et le YAML n'en porte que la REFERENCE (`keyring:SERVICE/COMPTE`).
   Sans trousseau — conteneur, service — on retombe sur `env:NOM` et l'assistant
   affiche les variables a poser.

4. LES VERIFICATIONS SONT PROPOSEES, JAMAIS IMPOSEES. On les devine en lisant
   `package.json` et `pyproject.toml`, et on les fait confirmer. Une commande de
   verification fausse ne casse rien de visible : elle fait juste refuser au
   demon de commiter du code pourtant bon, cycle apres cycle, sans que le motif
   ait quoi que ce soit a voir avec le code.

── POURQUOI PAS UN FICHIER CHIFFRE ─────────────────────────────────────────

C'est la demande naturelle et elle ne tient pas : un demon qui redemarre seul
doit dechiffrer seul, donc la cle est a sa portee, sur la meme machine. Chiffrer
avec une cle posee a cote du chiffre, c'est de l'obfuscation — et une
obfuscation prise pour du chiffrement est pire que rien, parce qu'on cesse de se
mefier. Le trousseau, lui, fait tenir la cle par le systeme. Voir `SecretRef`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

__all__ = ["assistant"]

SERVICE = "agent-runner-lg"
API = "https://api.github.com"


def en_conteneur() -> bool:
    """Tourne-t-on dans un conteneur ?

    Deux indices, et le second suffit a lui seul : `/.dockerenv` est pose par
    Docker, et `AGENT_RUNNER_WORKSPACE` par notre propre compose. On ne cherche
    pas a etre exhaustif — se tromper ne coute qu'un defaut propose, que
    l'utilisateur voit et peut changer.
    """
    return Path("/.dockerenv").exists() or bool(
        os.environ.get("AGENT_RUNNER_WORKSPACE", "").strip())


# ── Poser une question ──────────────────────────────────────────────────────

class Abandon(Exception):
    """L'utilisateur a interrompu, ou l'entree n'est pas un terminal."""


def _lire(invite: str) -> str:
    try:
        return input(invite)
    except (EOFError, KeyboardInterrupt) as e:
        raise Abandon("interrompu") from e


def demander(question: str, defaut: str = "", *, obligatoire: bool = True) -> str:
    while True:
        suffixe = f" [{defaut}]" if defaut else ""
        rep = _lire(f"  {question}{suffixe} : ").strip() or defaut
        if rep or not obligatoire:
            return rep
        print("     (une reponse est attendue)")


def demander_secret(question: str) -> str:
    """Lit un secret sans l'afficher. Sans terminal, `getpass` retombe sur stdin.

    On le DIT quand l'echo n'a pas pu etre coupe : croire qu'un jeton n'est pas
    apparu a l'ecran alors qu'il y est, c'est le laisser dans un historique de
    terminal ou dans une capture.
    """
    import getpass  # noqa: PLC0415

    try:
        val = getpass.getpass(f"  {question} : ")
    except (EOFError, KeyboardInterrupt) as e:
        raise Abandon("interrompu") from e
    except Exception:  # noqa: BLE001 — getpass leve large quand il n'a pas de tty
        print("     /!\\ l'echo n'a pas pu etre coupe : la saisie sera VISIBLE.")
        val = _lire(f"  {question} : ")
    return val.strip()


def demander_oui(question: str, *, defaut: bool = True) -> bool:
    d = "O/n" if defaut else "o/N"
    rep = _lire(f"  {question} [{d}] : ").strip().lower()
    if not rep:
        return defaut
    return rep[0] in "oy"


def cocher(titre: str, options: list[tuple[str, str]]) -> list[str]:
    """Choix multiple par numeros. Rend les cles retenues.

    Les numeros plutot que les noms : un nom de depot se tape mal, se trompe
    facilement, et la liste est deja affichee.
    """
    print(f"\n  {titre}")
    for i, (_, libelle) in enumerate(options, 1):
        print(f"    {i:2d}. {libelle}")
    while True:
        rep = _lire("  Numeros separes par des virgules (ou « tout ») : ").strip()
        if rep.lower() in ("tout", "all", "*"):
            return [c for c, _ in options]
        try:
            nums = [int(x) for x in rep.replace(" ", ",").split(",") if x]
        except ValueError:
            print("     (des numeros, separes par des virgules)")
            continue
        if nums and all(1 <= n <= len(options) for n in nums):
            return [options[n - 1][0] for n in nums]
        print(f"     (des numeros entre 1 et {len(options)})")


# ── La forge ────────────────────────────────────────────────────────────────

def verifier_jeton(jeton: str) -> dict[str, Any]:
    """Qui est ce jeton, et que peut-il ? Leve si la forge le refuse.

    On lit AUSSI les droits (`x-oauth-scopes`), parce qu'un jeton valide mais
    sans le bon perimetre echoue plus tard, sur une ecriture, avec un message
    qui ne nomme pas la cause.
    """
    r = httpx.get(f"{API}/user", timeout=15, headers={
        "authorization": f"Bearer {jeton}",
        "accept": "application/vnd.github+json",
    })
    if r.status_code == 401:
        raise ValueError("jeton refuse par GitHub (401) : expire, revoque, ou mal copie")
    r.raise_for_status()
    u = r.json()
    return {
        "login": u.get("login"),
        # Un PAT « fine-grained » ne rend AUCUN scope dans cet en-tete : absence
        # ne veut pas dire « aucun droit ». On le dit plutot que de l'interpreter.
        "scopes": r.headers.get("x-oauth-scopes", ""),
        "fine_grained": "x-oauth-scopes" not in r.headers,
    }


def lister_depots(jeton: str, org: str) -> list[dict[str, Any]]:
    """Les depots visibles par ce jeton dans cette organisation, recents d'abord."""
    entetes = {"authorization": f"Bearer {jeton}",
               "accept": "application/vnd.github+json"}
    depots: list[dict[str, Any]] = []
    for page in range(1, 4):                      # 300 depots, largement assez
        r = httpx.get(f"{API}/orgs/{org}/repos", timeout=20, headers=entetes,
                      params={"per_page": 100, "page": page, "sort": "updated"})
        if r.status_code == 404 and page == 1:
            # Pas une organisation : peut-etre un compte personnel.
            r = httpx.get(f"{API}/users/{org}/repos", timeout=20, headers=entetes,
                          params={"per_page": 100, "page": page, "sort": "updated"})
        r.raise_for_status()
        lot = r.json()
        depots += lot
        if len(lot) < 100:
            break
    return [d for d in depots if not d.get("archived")]


# ── Deviner les verifications ───────────────────────────────────────────────

# Les scripts npm qu'on propose, dans l'ordre ou on veut les voir tourner : ce
# qui echoue vite en premier. `run_checks` s'arrete a la premiere commande rouge,
# donc l'ordre decide de ce qu'on apprend d'un echec.
_NPM = ["typecheck", "lint", "test:ci", "test", "build"]


def deviner_checks(chemin: Path) -> list[str]:
    """Propose des verifications en lisant le depot. Jamais imposees.

    Une commande fausse ne casse rien de visible : elle fait refuser au demon de
    commiter du code pourtant bon, cycle apres cycle, pour un motif qui n'a rien
    a voir avec le code. C'est pour ca qu'on les fait confirmer.
    """
    out: list[str] = []
    pkg = chemin / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, json.JSONDecodeError):
            scripts = {}
        out += [f"npm run {s}" for s in _NPM if s in scripts]

    pyproject = chemin / "pyproject.toml"
    if pyproject.is_file():
        try:
            conf = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            conf = {}
        outils = conf.get("tool", {})
        if "ruff" in outils:
            out.append("ruff check .")
        if "pytest" in outils or (chemin / "tests").is_dir():
            out.append("python -m pytest -q")
    elif (chemin / "tests").is_dir() and any(chemin.glob("*.py")):
        out.append("python -m pytest -q")
    return out


# ── Ecrire ──────────────────────────────────────────────────────────────────

def sauvegarder(chemin: Path) -> Path | None:
    """Met un fichier existant de cote avant de le remplacer. Rend son chemin."""
    if not chemin.exists():
        return None
    horodatage = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    copie = chemin.with_suffix(chemin.suffix + f".{horodatage}.bak")
    shutil.copy2(chemin, copie)
    return copie


def poser_secret(compte: str, valeur: str, *, trousseau: bool) -> str:
    """Range le secret et rend sa REFERENCE, jamais sa valeur.

    Sans trousseau, on ne stocke rien : on rend une reference `env:` et
    l'appelant affichera la variable a poser. Ecrire le secret dans un fichier
    « au cas ou » annulerait tout l'interet.
    """
    if not trousseau:
        return f"env:{compte}"
    import keyring  # noqa: PLC0415

    keyring.set_password(SERVICE, compte, valeur)
    return f"keyring:{SERVICE}/{compte}"


def trousseau_disponible() -> bool:
    try:
        import keyring  # noqa: PLC0415
        from keyring.backends.fail import Keyring as SansDorsale  # noqa: PLC0415
    except ImportError:
        return False
    try:
        return not isinstance(keyring.get_keyring(), SansDorsale)
    except Exception:  # noqa: BLE001
        return False


# ── Les fichiers ────────────────────────────────────────────────────────────

def _yaml_runner(*, racine: Path, port: int, oauth: str, arme: bool,
                 conteneur: bool = False) -> str:
    # En conteneur, la frontiere n'est plus la boucle locale mais le NAMESPACE
    # RESEAU : `127.0.0.1` a l'interieur n'est joignable par personne, meme avec
    # une publication de port. Ecouter sur 0.0.0.0 et publier cote hote en
    # 127.0.0.1 donne exactement la meme surface qu'une ecoute locale.
    ecoute = ("bind: 0.0.0.0\n  # Le conteneur EST la frontiere. La publication "
              "cote hote doit rester\n  # sur 127.0.0.1 — sinon la console "
              "s'ouvre a tout le reseau.\n  reseau_confine: true"
              if conteneur else "bind: 127.0.0.1")
    return f"""# La MACHINE, pas les projets. Un seul de ces fichiers, jamais copie.
# Ecrit par `agent-runner-lg init`. Relisez-le : rien ici n'est irreversible,
# mais tout y est lu au demarrage.

# HORS d'un repertoire cache, et ce n'est pas une preference de rangement : un
# worktree monte sous un dossier commencant par un point casse la decouverte de
# tests de tout outil qui construit un glob depuis sa racine. Mesure : jest
# rendait « No tests found » pendant que les memes tests passaient dans le depot
# principal.
worktrees_root: {racine.as_posix()}/worktrees
state_db:       {racine.as_posix()}/state.db
logs_dir:       {racine.as_posix()}/logs
profiles_dir:   ./profils

# L'API locale sert la console. Elle n'ecoute QUE sur la boucle locale, et la
# validation refuse 0.0.0.0 : le demon n'expose aucun port entrant.
api:
  {ecoute}
  port: {port}

wake:
  poll_wait: 50s
  reconcile_every: 5m

claude:
  # Repertoire de configuration DEDIE : isole le demon de la configuration
  # personnelle et de la memoire automatique, qui polluerait le contexte d'un
  # projet avec celui d'un autre.
  config_dir: {racine.as_posix()}/.claude
  oauth_token: {oauth}
  disable_auto_memory: true

  # ANTHROPIC_API_KEY passe AVANT le jeton OAuth dans l'ordre de precedence : une
  # variable qui traine bascule la facturation de l'abonnement vers l'API, sans
  # rien afficher. On l'efface ; on ne parie pas sur son absence.
  scrub_env:
    - ANTHROPIC_API_KEY
    - ANTHROPIC_AUTH_TOKEN

# DEUX CRANS, PAS UN.
#   false -> le demon lit, decide, construit le prompt, et s'arrete la.
#   true  -> l'agent tourne : worktree, correctif, verifications, commit. Le
#            PUSH depend d'un second interrupteur, `token_write` dans le profil.
writes_enabled: {str(arme).lower()}

# Jobs menes de front. Jamais deux sur un MEME depot — ils partagent son `.git`.
# Le parallelisme utile est ENTRE depots.
max_parallel: 3
"""


def _yaml_profil(*, projet: str, org: str, workspace: Path, lecture: str,
                 ecriture: str | None, notify: str, relecteurs: list[str],
                 depots: list[dict[str, Any]],
                 auteurs: list[str] | None = None) -> str:
    lignes = [
        f"# Le projet « {projet} ». Copier ce fichier suffit a ajouter un projet :",
        "# le moteur n'est pas touche.",
        "#",
        "# Ecrit par `agent-runner-lg init`.",
        "",
        f"project: {projet}",
        f"workspace: {workspace.as_posix()}",
        "",
        "forge:",
        f"  org: {org}",
        f"  token_read: {lecture}",
    ]
    if ecriture:
        lignes.append(f"  token_write: {ecriture}     # depots `access: write` uniquement")
    else:
        lignes += [
            "  # token_write absent : l'agent corrigera dans son worktree, sans",
            "  # pousser, sans ouvrir de PR et sans repondre aux fils. C'est le",
            "  # cran ou l'on relit le travail avant qu'il devienne visible.",
        ]

    lignes += [
        "",
        "# Le nombre de fois qu'une meme PR peut repasser par l'agent. Au-dela,",
        "# la boucle ne converge pas : on appelle un humain.",
        "max_review_cycles: 3",
        "",
        "reviewers:",
        "  # QUI a le droit de faire travailler l'agent. Cette liste vient du",
        "  # profil, JAMAIS de la charge utile : c'est elle qui empeche un",
        "  # commentaire quelconque de declencher un cycle paye.",
        "  trust:",
    ]
    lignes += [f"    - {r}" for r in relecteurs] or ["    []"]

    lignes.append("")
    if auteurs:
        lignes += [
            "# QUELLES PR ce demon prend en charge.",
            "#",
            "# Les baux vivent dans une base sqlite LOCALE : deux demons sur deux",
            "# machines n'ont AUCUNE exclusion mutuelle. Sans ce perimetre, ils",
            "# prendraient la meme PR, pousseraient sur la meme branche et",
            "# repondraient deux fois dans les memes fils.",
            "#",
            "# Vide = toutes les PR. C'est le bon reglage pour un demon UNIQUE,",
            "# partage par une equipe sous une identite de service.",
            "scope:",
            "  authors:",
        ]
        lignes += [f"    - {a}" for a in auteurs]
        lignes.append("")

    if notify:
        lignes += ["human:", f'  notify: "{notify}"']
    else:
        # Pas de cle `human:` du tout : une cle presente et vide vaut `null`,
        # que la validation refuse. L'absence, elle, laisse le defaut jouer.
        lignes += [
            "# human.notify absent : une question posee dans un fil ne mentionnera",
            "# personne, donc personne ne sera prevenu. A completer.",
        ]
    lignes += ["", "repos:"]
    for d in depots:
        lignes += [
            f"  {d['nom']}:",
            f"    access: {d['access']}",
            f"    path: {Path(d['path']).as_posix()}",
        ]
        if d["access"] == "write":
            if d["checks"]:
                lignes.append("    checks:")
                lignes += [f"      - \"{c}\"" for c in d["checks"]]
            else:
                lignes += [
                    "    # AUCUNE verification : le correctif sera commite sans",
                    "    # avoir ete valide localement. A completer.",
                    "    checks: []",
                ]
        lignes.append("")
    return "\n".join(lignes).rstrip() + "\n"


# ── L'assistant ─────────────────────────────────────────────────────────────

def _titre(texte: str) -> None:
    print()
    print(f"  == {texte} " + "=" * max(0, 58 - len(texte)))


def assistant(chemin_runner: Path) -> int:
    """Pose les questions, verifie, ecrit. Rend le code de sortie."""
    if not sys.stdin.isatty():
        print("`init` est interactif : le lancer depuis un terminal.", file=sys.stderr)
        return 2

    print()
    print("  Assistant d'installation d'agent-runner-lg.")
    print("  Rien n'est ecrit avant la derniere question, et un fichier existant")
    print("  est toujours sauvegarde avant d'etre remplace.")

    dossier = chemin_runner.parent.resolve()
    conteneur = en_conteneur()
    if conteneur:
        print()
        print("  Conteneur detecte. Les defauts proposes sont ceux du conteneur :")
        print("  etat sous /var/agent-runner, depots sous /repos, console ouverte")
        print("  sur le namespace reseau (la publication cote hote la borne).")

    # ── 1. Ou vivent l'etat et les journaux ────────────────────────────────
    _titre("Ou ranger l'etat du demon")
    print("  Baux, journaux, worktrees et configuration Claude dediee.")
    print("  HORS d'un repertoire cache : un worktree sous un dossier commencant")
    print("  par un point casse la decouverte de tests de jest et consorts.")
    defaut_racine = "/var/agent-runner" if conteneur else str(dossier / "var")
    racine = Path(demander("Racine", defaut_racine)).expanduser()
    if not conteneur:
        racine = racine.resolve()
    if any(p.startswith(".") for p in racine.parts):
        print("     !! ce chemin passe par un repertoire cache. `check` le signalera.")

    # ── 2. Ou ranger les secrets ───────────────────────────────────────────
    _titre("Ou ranger les secrets")
    if trousseau_disponible():
        print("  Trousseau du systeme detecte. C'est le bon endroit : la cle est")
        print("  tenue par le systeme, jamais posee dans le dossier du projet.")
        trousseau = demander_oui("Employer le trousseau ?")
    else:
        print("  Aucun trousseau utilisable (conteneur, service, ou `keyring`")
        print("  absent). Les secrets seront references par variable")
        print("  d'environnement, et l'assistant listera celles a poser.")
        trousseau = False

    a_poser: list[tuple[str, str]] = []          # (variable, a quoi elle sert)

    def ranger(compte: str, valeur: str, role: str) -> str:
        ref = poser_secret(compte, valeur, trousseau=trousseau)
        if ref.startswith("env:"):
            a_poser.append((compte, role))
        return ref

    # ── 3. La forge ────────────────────────────────────────────────────────
    _titre("La forge")
    org = demander("Organisation ou compte GitHub")

    jeton_lecture = demander_secret("Jeton de LECTURE")
    try:
        qui = verifier_jeton(jeton_lecture)
    except (ValueError, httpx.HTTPError) as e:
        print(f"  Le jeton n'a pas ete accepte : {e}", file=sys.stderr)
        return 2
    droits = "" if qui["fine_grained"] else f", droits : {qui['scopes'] or 'aucun'}"
    print(f"     jeton valide — compte « {qui['login']} »{droits}")
    if qui["fine_grained"]:
        print("     (jeton fine-grained : GitHub n'annonce pas ses droits ici.")
        print("      `check` dira ce qui manque une fois la configuration ecrite.)")
    ref_lecture = ranger("PAT_READ", jeton_lecture, "lecture de la forge")

    _titre("Le jeton d'ecriture")
    print("  Sans lui, l'agent corrige dans son worktree, sans pousser, sans")
    print("  ouvrir de PR et sans repondre aux fils. C'est le cran ou l'on relit")
    print("  le travail avant qu'il devienne visible — un bon mode pour commencer.")
    ref_ecriture = None
    if demander_oui("Poser un jeton d'ecriture maintenant ?", defaut=False):
        jeton_ecriture = demander_secret("Jeton d'ECRITURE")
        try:
            qui_w = verifier_jeton(jeton_ecriture)
        except (ValueError, httpx.HTTPError) as e:
            print(f"  Le jeton n'a pas ete accepte : {e}", file=sys.stderr)
            return 2
        print(f"     jeton valide — compte « {qui_w['login']} »")
        ref_ecriture = ranger("PAT_WRITE", jeton_ecriture, "ecriture sur la forge")

    # ── 4. Les depots ──────────────────────────────────────────────────────
    _titre("Les depots")
    try:
        depots_api = lister_depots(jeton_lecture, org)
    except httpx.HTTPError as e:
        print(f"  Impossible de lister les depots de « {org} » : {e}", file=sys.stderr)
        return 2
    if not depots_api:
        print(f"  Aucun depot visible dans « {org} » avec ce jeton.", file=sys.stderr)
        return 2

    # En conteneur, les depots sont dans le volume : ni le dossier de l'hote ni
    # ses chemins n'ont de sens ici.
    defaut_ws = "/repos" if conteneur else str(dossier.parent)
    workspace = Path(demander("Ou sont les copies locales", defaut_ws)).expanduser()
    if not conteneur:
        workspace = workspace.resolve()

    options = []
    for d in depots_api:
        nom = d["name"]
        absent = "" if (workspace / nom).is_dir() else "   (pas de copie locale)"
        options.append((nom, f"{nom:<24}{d.get('language') or '-':<12}{absent}"))
    retenus = cocher(f"{len(options)} depot(s) dans « {org} ». Lesquels suivre ?",
                     options)

    depots: list[dict[str, Any]] = []
    for nom in retenus:
        _titre(f"Depot « {nom} »")
        chemin = Path(demander("Copie locale", str(workspace / nom))).expanduser()
        if not chemin.is_dir():
            print("     !! introuvable : aucun worktree ne pourra en etre derive.")
        print("  write   : l'agent y corrige et y pousse")
        print("  context : l'agent le LIT — code, conventions — sans pouvoir l'ecrire")
        acces = "write" if demander_oui("Modifiable par l'agent ?") else "context"

        checks: list[str] = []
        if acces == "write":
            devines = deviner_checks(chemin) if chemin.is_dir() else []
            if devines:
                print("  Verifications proposees, dans l'ordre ou elles tourneront :")
                for c in devines:
                    print(f"    - {c}")
                print("  (le premier rouge arrete la serie : l'ordre decide de ce")
                print("   qu'on apprend d'un echec)")
                checks = devines if demander_oui("Les garder ?") else []
            if not checks:
                print("  Une commande par ligne, ligne vide pour finir.")
                while (c := demander("  check", obligatoire=False)):
                    checks.append(c)
        depots.append({"nom": nom, "path": str(chemin), "access": acces,
                       "checks": checks})

    # ── 5. Le perimetre ────────────────────────────────────────────────────
    _titre("Quelles PR ce demon prend en charge")
    print("  Si plusieurs personnes lancent chacune leur demon sur les MEMES")
    print("  depots, ils se marchent dessus : les baux sont locaux, donc il n'y")
    print("  a aucune exclusion entre machines. Deux agents prendraient la meme")
    print("  PR, pousseraient sur la meme branche, repondraient deux fois.")
    print("  Restreindre par auteur rend les ensembles de travail disjoints.")
    auteurs: list[str] = []
    if demander_oui(f"Ne traiter que les PR ouvertes par « {qui['login']} » ?"):
        auteurs = [qui["login"]]
    else:
        print("  Vide = toutes les PR. A ne laisser vide que s'il n'y a QU'UN")
        print("  demon sur ces depots.")
        auteurs = [x.strip() for x in
                   demander("Logins, separes par des virgules",
                            obligatoire=False).split(",") if x.strip()]

    # ── 6. Qui fait travailler l'agent ─────────────────────────────────────
    _titre("Les relecteurs de confiance")
    print("  QUI a le droit de declencher un cycle. Cette liste vient du profil,")
    print("  JAMAIS de la charge utile : c'est elle qui empeche un commentaire")
    print("  quelconque de faire travailler l'agent — et de consommer du quota.")
    print("  Exemples : chatgpt-codex-connector, github-copilot, un login humain.")
    relecteurs = [x.strip() for x in
                  demander("Logins separes par des virgules").split(",") if x.strip()]

    _titre("Qui prevenir")
    print("  Sans mention, une question posee dans un fil ne notifie personne.")
    notify = demander("Mention (ex. @moi)", obligatoire=False)

    # ── 7. Le moteur ───────────────────────────────────────────────────────
    _titre("Le moteur")
    oauth = demander_secret("Jeton OAuth Claude (CLAUDE_CODE_OAUTH_TOKEN)")
    ref_oauth = ranger("CLAUDE_CODE_OAUTH_TOKEN", oauth, "identifiant du SDK Claude")

    # ── 8. Ecriture ────────────────────────────────────────────────────────
    _titre("Ecriture")
    projet = demander("Nom du projet", org.lower())
    chemin_profil = dossier / "profils" / f"{projet}.yaml"
    print(f"  {chemin_runner}")
    print(f"  {chemin_profil}")
    if not demander_oui("Ecrire ces deux fichiers ?"):
        print("  Rien n'a ete ecrit.")
        return 1

    for cible, contenu in (
        (chemin_runner, _yaml_runner(racine=racine, port=8788, oauth=ref_oauth,
                                     arme=False, conteneur=conteneur)),
        (chemin_profil, _yaml_profil(projet=projet, org=org, workspace=workspace,
                                     lecture=ref_lecture, ecriture=ref_ecriture,
                                     notify=notify, relecteurs=relecteurs,
                                     depots=depots, auteurs=auteurs)),
    ):
        cible.parent.mkdir(parents=True, exist_ok=True)
        if copie := sauvegarder(cible):
            print(f"  ancien fichier sauvegarde : {copie}")
        cible.write_text(contenu, encoding="utf-8")
        print(f"  ecrit : {cible}")

    print()
    print("  `writes_enabled: false` : le demon lit, decide, construit le prompt")
    print("  et s'arrete. C'est voulu — on observe avant d'armer.")
    if a_poser:
        print()
        print("  Variables d'environnement a poser AVANT de lancer le demon :")
        for var, role in a_poser:
            print(f"    {var:<28} {role}")
    print()
    print("  Ensuite :")
    print("    agent-runner-lg -c runner.yaml check     valide et dit ce qui manque")
    print("    agent-runner-lg -c runner.yaml status    ce que le demon ferait")
    print("    agent-runner-lg -c runner.yaml serve     le demon + la console")
    return 0
