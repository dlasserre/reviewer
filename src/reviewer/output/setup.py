"""Le mode INSTALLATION : ce que le demon sert tant qu'il n'a pas de configuration.

`docker compose up`, on ouvre la console, on colle un jeton, on coche des
depots. Le reste — ecrire la configuration, cloner, installer les dependances —
se fait tout seul.

── LE PRINCIPE QU'ON INFLECHIT, ET COMMENT ─────────────────────────────────

`console.py` dit que la console ne modifie AUCUN reglage, parce qu'un agent dont
les droits changent sans trace n'a plus de droits, il a des habitudes. C'est
vrai — pour un demon CONFIGURE.

Ca ne vaut pas pour un demon qui n'a encore rien a proteger. Ici il n'y a ni
depot declare, ni jeton, ni armement : il n'existe aucun droit a elargir.

L'inflexion est donc bornee, et par la STRUCTURE plutot que par une promesse :
ce module n'est monte que lorsque `runner.yaml` est absent. Une fois la
configuration ecrite, le demon sert `api.py`, qui n'a aucune route d'ecriture.
Le chemin de code n'existe plus.

── LES SECRETS ─────────────────────────────────────────────────────────────

Les jetons saisis ici finissent dans `<config>/.secrets.env`, en clair, lu au
demarrage. C'est EXACTEMENT ce qu'un `.env` expose — ni plus, ni moins — et il
faut le dire ainsi : il n'y a pas de trousseau dans un conteneur, et chiffrer
avec une cle que le demon doit pouvoir lire ne protegerait de rien.

Le YAML, lui, ne porte toujours que des references `env:NOM`.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from reviewer.bootstrap import (API, deviner_checks, deviner_setup,
                                verifier_jeton, _yaml_profil, _yaml_runner)
from reviewer.output.setup_page import PAGE_SETUP
from reviewer.repo.provision import assurer_outillage

__all__ = ["FICHIER_SECRETS", "charger_secrets", "create_setup_app"]

FICHIER_SECRETS = ".secrets.env"


# ── Les secrets, a cote de la configuration ─────────────────────────────────

def charger_secrets(dossier: Path) -> int:
    """Verse `<config>/.secrets.env` dans l'environnement. Rend le nombre de cles.

    Les valeurs deja presentes dans l'environnement GAGNENT : un secret injecte
    par l'orchestrateur — Docker secrets, variable de service — doit primer sur
    un fichier ecrit un jour par la console. Sinon on ne saurait plus lequel des
    deux tourne.
    """
    f = dossier / FICHIER_SECRETS
    if not f.is_file():
        return 0
    n = 0
    for ligne in f.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, _, val = ligne.partition("=")
        cle, val = cle.strip(), val.strip()
        if cle and val and not os.environ.get(cle):
            os.environ[cle] = val
            n += 1
    return n


def ecrire_secrets(dossier: Path, secrets: dict[str, str]) -> Path:
    f = dossier / FICHIER_SECRETS
    lignes = [
        "# Ecrit par la console, au moment de l'installation.",
        "# EN CLAIR — exactement comme un `.env`, ni plus ni moins. Il n'y a pas",
        "# de trousseau dans un conteneur, et chiffrer avec une cle que le demon",
        "# doit pouvoir lire ne protegerait de rien.",
        "#",
        "# Une variable deja presente dans l'environnement gagne sur ce fichier.",
        "",
    ]
    lignes += [f"{k}={v}" for k, v in secrets.items() if v]
    f.write_text("\n".join(lignes) + "\n", encoding="utf-8")
    try:
        f.chmod(0o600)          # sans effet sous Windows, utile ailleurs
    except OSError:
        pass
    return f


# ── Ce que la page envoie ───────────────────────────────────────────────────

class Jetons(BaseModel):
    token_read: str
    token_write: str = ""
    oauth: str = ""
    org: str


class Installation(BaseModel):
    org: str
    projet: str
    token_read: str
    token_write: str = ""
    oauth: str = ""
    depots: list[dict[str, Any]]        # {nom, access}
    relecteurs: list[str] = []
    notify: str = ""
    auteurs: list[str] = []


# ── L'application ───────────────────────────────────────────────────────────

def commande_de_clonage(url: str, cible: Path) -> list[str]:
    """Le clone d'installation — et pourquoi il n'est PAS mono-branche.

    `--depth` implique `--single-branch`. Le clone n'ecrit alors qu'un seul
    refspec (`+refs/heads/<defaut>:refs/remotes/origin/<defaut>`), et plus aucun
    `git fetch origin` ne ramenera jamais autre chose. Un demon dont le travail
    est de recuperer la tete d'une PR se retrouve ainsi sans aucune tete de PR.

    Mesure du 30/08/2026, dans le conteneur : `fatal: invalid reference:
    origin/dev` au montage du worktree de `frontend#406`. Le message designe
    `dev`, mais `origin/hotfix/405-...` manquait tout autant — le clone
    n'avait que la branche par defaut.

    `--no-single-branch` retablit `+refs/heads/*:refs/remotes/origin/*` en
    GARDANT la profondeur : toutes les branches, tronquees a 50 commits. C'est
    suffisant ici, `diff_stat` comparant l'arbre a `HEAD` et jamais a une base
    lointaine.
    """
    return ["git", "clone", "--no-single-branch", "--depth", "50",
            url, str(cible)]


def create_setup_app(chemin_runner: Path, *, port: int = 8788) -> FastAPI:
    """L'application servie tant qu'il n'y a pas de configuration."""
    dossier = chemin_runner.parent
    app = FastAPI(title="reviewer — installation")
    # Le journal de l'installation. Une liste, pas un fichier : si le clonage
    # echoue, ce qu'on veut lire est a l'ecran, pas sur un disque qu'il faudra
    # aller ouvrir.
    progres: list[dict[str, str]] = []
    fini = asyncio.Event()

    def dire(genre: str, texte: str) -> None:
        progres.append({"genre": genre, "texte": texte})

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "configured": False, "config_path": str(chemin_runner)}

    @app.get("/", include_in_schema=False)
    def page() -> HTMLResponse:
        return HTMLResponse(PAGE_SETUP)

    @app.post("/api/depots")
    def depots(j: Jetons) -> dict[str, Any]:
        """Valide le jeton et rend les depots visibles. AVANT toute ecriture.

        Une configuration qui reference un jeton mort a l'air juste et echoue
        plus tard, dans un message qui parle d'autre chose.
        """
        try:
            qui = verifier_jeton(j.token_read)
        except (ValueError, httpx.HTTPError) as e:
            raise HTTPException(400, f"jeton refuse : {e}") from e

        entetes = {"authorization": f"Bearer {j.token_read}",
                   "accept": "application/vnd.github+json"}
        trouves: list[dict[str, Any]] = []
        for chemin in (f"{API}/orgs/{j.org}/repos", f"{API}/users/{j.org}/repos"):
            try:
                r = httpx.get(chemin, timeout=20, headers=entetes,
                              params={"per_page": 100, "sort": "updated"})
            except httpx.HTTPError as e:
                raise HTTPException(502, f"la forge n'a pas repondu : {e}") from e
            if r.status_code == 404:
                continue
            if r.is_error:
                raise HTTPException(400, f"{j.org} : {r.text[:200]}")
            trouves = [d for d in r.json() if not d.get("archived")]
            break
        if not trouves:
            raise HTTPException(404, f"aucun depot visible dans « {j.org} »")

        return {
            "login": qui["login"],
            "fine_grained": qui["fine_grained"],
            "depots": [{"nom": d["name"], "langage": d.get("language") or "",
                        "prive": bool(d.get("private"))} for d in trouves],
        }

    @app.post("/api/installer")
    async def installer(inst: Installation) -> dict[str, str]:
        """Ecrit la configuration, puis clone et installe — en tache de fond."""
        if chemin_runner.exists():
            raise HTTPException(409, "une configuration existe deja")
        progres.clear()
        fini.clear()
        asyncio.create_task(_poser(inst))
        return {"lance": "ok"}

    @app.get("/api/progres")
    async def api_progres() -> StreamingResponse:
        async def flux() -> AsyncIterator[bytes]:
            vus = 0
            while True:
                while vus < len(progres):
                    yield f"data: {json.dumps(progres[vus], ensure_ascii=False)}\n\n".encode()
                    vus += 1
                if fini.is_set() and vus >= len(progres):
                    yield b"data: {\"genre\": \"fin\", \"texte\": \"\"}\n\n"
                    return
                await asyncio.sleep(.4)

        return StreamingResponse(flux(), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache",
                                          "x-accel-buffering": "no"})

    # ── Le travail ──────────────────────────────────────────────────────────

    async def _poser(inst: Installation) -> None:
        try:
            racine = Path("/var/agent-runner") if Path("/.dockerenv").exists() \
                else (dossier / "var")
            espace = Path("/repos") if Path("/.dockerenv").exists() \
                else (dossier / "repos")
            espace.mkdir(parents=True, exist_ok=True)

            dire("etape", "Secrets")
            ecrire_secrets(dossier, {
                "PAT_READ": inst.token_read,
                "PAT_WRITE": inst.token_write,
                "CLAUDE_CODE_OAUTH_TOKEN": inst.oauth,
            })
            charger_secrets(dossier)
            dire("ok", f"{FICHIER_SECRETS} ecrit — en clair, comme un .env")

            dire("etape", "Depots")
            for d in inst.depots:
                await _cloner(inst, d["nom"], espace)

            dire("etape", "Configuration")
            # On redemande QUI est ce jeton plutot que de croire la page : c'est
            # cette identite qui signera les commits, et une valeur venue du
            # navigateur n'est pas une valeur verifiee.
            try:
                login = verifier_jeton(inst.token_read)["login"]
            except Exception:  # noqa: BLE001 — degrade, ne bloque pas
                login = ""
                dire("erreur", "identite de commit indeterminee : "
                               "a completer dans le profil (`commit:`)")
            chemin_runner.write_text(
                _yaml_runner(racine=racine, port=port,
                             oauth="env:CLAUDE_CODE_OAUTH_TOKEN", arme=False,
                             conteneur=Path("/.dockerenv").exists()),
                encoding="utf-8")
            profils = dossier / "profils"
            profils.mkdir(exist_ok=True)
            (profils / f"{inst.projet}.yaml").write_text(
                _yaml_profil(
                    projet=inst.projet, org=inst.org, workspace=espace,
                    lecture="env:PAT_READ",
                    ecriture="env:PAT_WRITE" if inst.token_write else None,
                    notify=inst.notify, relecteurs=inst.relecteurs,
                    auteurs=inst.auteurs,
                    depots=[{"nom": d["nom"], "access": d["access"],
                             "path": str(espace / d["nom"]),
                             "checks": deviner_checks(espace / d["nom"])
                             if d["access"] == "write" else [],
                             "setup": deviner_setup(espace / d["nom"])
                             if d["access"] == "write" else []}
                            for d in inst.depots],
                    commit_login=login or None),
                encoding="utf-8")
            dire("ok", f"runner.yaml et profils/{inst.projet}.yaml ecrits")
            dire("termine", "Installation faite. Le demon redemarre.")
            reussi = True
        except Exception as e:  # noqa: BLE001 — tout doit atterrir a l'ecran
            dire("erreur", f"{type(e).__name__} : {e}")
            reussi = False
        finally:
            fini.set()

        if reussi:
            # On s'ARRETE, on ne recharge pas a chaud. Relire la configuration
            # en cours de route creerait un instant ou une partie du demon
            # tourne sur l'ancienne et une autre sur la nouvelle — et ce genre
            # d'instant ne se debogue pas.
            #
            # `restart: unless-stopped` releve le conteneur, qui repart
            # configure. Hors conteneur, le message a l'ecran dit quoi relancer.
            await asyncio.sleep(2.0)      # laisser le flux delivrer sa fin
            print("Configuration ecrite. Le processus s'arrete pour repartir "
                  "avec elle.")
            os._exit(0)

    async def _cloner(inst: Installation, nom: str, espace: Path) -> None:
        cible = espace / nom
        if (cible / ".git").is_dir():
            dire("ok", f"{nom} — deja clone")
        else:
            url = (f"https://x-access-token:{inst.token_read}"
                   f"@github.com/{inst.org}/{nom}.git")
            dire("cours", f"{nom} — clonage")
            code, sortie = await _executer(commande_de_clonage(url, cible))
            if code != 0:
                # Le jeton est DANS l'URL : ne jamais renvoyer la sortie brute.
                dire("erreur", f"{nom} — clonage impossible (code {code})")
                return
            dire("ok", f"{nom} — clone")

        # Les dependances, devinees. Sans elles, les verifications echouent et
        # le demon refuse de commiter du code pourtant bon.
        #
        # PASSE PAR `assurer_outillage`, comme les jobs : c'est ce qui installe
        # dans un VENV DU DEPOT au lieu du Python du systeme. Le `pip install`
        # d'avant visait `site-packages`, que l'utilisateur du conteneur (uid
        # 1000) ne peut pas ecrire — il echouait, la page affichait une ligne
        # rouge qui defilait, et le premier job mourait en « code 127 ».
        setup = deviner_setup(cible)
        if not setup:
            return
        dire("cours", f"{nom} — outillage ({len(setup)} commande(s))")
        resultat = await asyncio.to_thread(
            assurer_outillage, cible, setup,
            on_result=lambda ligne: dire("ok", f"{nom} — {ligne}"),
        )
        for commande, sortie in resultat.echouees:
            dire("erreur", f"{nom} — {commande} : {sortie[-200:]}")

    async def _executer(commande: list[str], cwd: Path | None = None
                        ) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *commande, cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        brut, _ = await proc.communicate()
        return proc.returncode or 0, brut.decode("utf-8", "replace")

    return app
