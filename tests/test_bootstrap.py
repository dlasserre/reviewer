"""L'assistant d'installation ecrit-il une configuration que le demon accepte ?

C'est la seule question qui compte ici, et elle a une reponse verifiable :
generer le YAML, puis le charger avec les VRAIS validateurs. Un assistant qui
produit un fichier refuse au demarrage est pire qu'aucun assistant — il donne
l'impression que l'installation a reussi.

Les autres tests portent sur les trois endroits ou l'on peut perdre un secret
ou du travail : la forme des references, la sauvegarde avant ecrasement, et la
detection des verifications.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from reviewer.bootstrap import (deviner_checks, poser_secret,
                                       sauvegarder, _yaml_profil, _yaml_runner)
from reviewer.config import ConfigError, SecretRef, load_profile, load_runner
from reviewer.output.setup import commande_de_clonage


# ── Ce que l'assistant ecrit doit se recharger ──────────────────────────────

DEPOTS = [
    {"nom": "backend", "path": "/ws/backend", "access": "write",
     "checks": ["ruff check .", "python -m pytest -q"]},
    {"nom": "docs", "path": "/ws/docs", "access": "context", "checks": []},
]


def _ecrire(tmp_path: Path, **kw) -> tuple[Path, Path]:
    runner = tmp_path / "runner.yaml"
    profil = tmp_path / "profils" / "essai.yaml"
    profil.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text(_yaml_runner(
        racine=tmp_path / "var", port=8788,
        oauth="env:CLAUDE_CODE_OAUTH_TOKEN", arme=False), encoding="utf-8")
    reglages = dict(projet="essai", org="UneOrg", workspace=tmp_path / "ws",
                    lecture="env:PAT_READ", ecriture="env:PAT_WRITE",
                    notify="@moi", relecteurs=["un-bot"], depots=DEPOTS)
    reglages.update(kw)
    profil.write_text(_yaml_profil(**reglages), encoding="utf-8")
    return runner, profil


def test_la_configuration_ecrite_se_recharge(tmp_path):
    runner_path, profil_path = _ecrire(tmp_path)
    runner = load_runner(runner_path)
    profil = load_profile(profil_path)

    assert runner.writes_enabled is False, "on observe avant d'armer"
    assert profil.project == "essai"
    assert profil.repos["backend"].access.value == "write"
    assert profil.repos["docs"].access.value == "context"
    assert profil.repos["backend"].checks == ["ruff check .", "python -m pytest -q"]
    assert profil.reviewers.trust == ["un-bot"]
    assert profil.human.notify == "@moi"


def test_sans_jeton_d_ecriture_le_profil_reste_valide(tmp_path):
    # C'est le cran intermediaire, et le plus employe au demarrage : l'agent
    # corrige sans rien rendre visible. Il doit se charger comme les autres.
    _, profil_path = _ecrire(tmp_path, ecriture=None)
    profil = load_profile(profil_path)
    assert profil.forge.token_write is None
    assert profil.forge.token_read == "env:PAT_READ"


def test_sans_mention_la_cle_human_est_ABSENTE(tmp_path):
    # Une cle presente et vide vaut `null`, ce que la validation refuse. C'est
    # exactement ce que produisait la premiere version.
    _, profil_path = _ecrire(tmp_path, notify="")
    assert "human:" not in profil_path.read_text(encoding="utf-8")
    assert load_profile(profil_path).human.notify is None


def test_un_depot_sans_check_le_DIT_dans_le_fichier(tmp_path):
    # Un depot en ecriture sans verification fait commiter du code jamais
    # valide. Le fichier doit le porter en toutes lettres, pas laisser une
    # liste vide muette.
    _, profil_path = _ecrire(tmp_path, depots=[
        {"nom": "backend", "path": "/ws/backend", "access": "write", "checks": []}])
    texte = profil_path.read_text(encoding="utf-8")
    assert "AUCUNE verification" in texte
    assert load_profile(profil_path).repos["backend"].checks == []


# ── Les references de secret ────────────────────────────────────────────────

def test_les_deux_formes_de_reference_sont_acceptees():
    assert SecretRef._valider("env:PAT_READ").source == "env"
    r = SecretRef._valider("keyring:agent-runner/PAT_READ")
    assert (r.source, r.var) == ("keyring", "agent-runner/PAT_READ")


@pytest.mark.parametrize("mauvais", [
    "ghp_unSecretEnClair",      # le cas qui compte : jamais dans un fichier
    "keyring:sans_barre",       # service sans compte
    "env:",
    "keyring:",
])
def test_une_reference_mal_formee_est_REFUSEE(mauvais):
    with pytest.raises(ValueError):
        SecretRef._valider(mauvais)


def test_le_trousseau_est_lu_a_la_resolution(monkeypatch):
    lus = []

    class FauxTrousseau:
        @staticmethod
        def get_password(service, compte):
            lus.append((service, compte))
            return "s3cr3t"

    monkeypatch.setitem(sys.modules, "keyring", FauxTrousseau)
    assert SecretRef._valider("keyring:svc/compte").resolve() == "s3cr3t"
    assert lus == [("svc", "compte")]


def test_un_trousseau_vide_leve_avec_de_quoi_agir(monkeypatch):
    # Un secret absent doit dire COMMENT le poser. Un « None » nu enverrait
    # chercher la panne dans le code du demon.
    class Vide:
        @staticmethod
        def get_password(service, compte):
            return None

    monkeypatch.setitem(sys.modules, "keyring", Vide)
    with pytest.raises(ConfigError) as e:
        SecretRef._valider("keyring:svc/compte").resolve()
    assert "init" in str(e.value)


def test_sans_le_paquet_keyring_le_message_nomme_le_conteneur(monkeypatch):
    # Le cas courant : la meme configuration lancee en conteneur, ou aucune
    # dorsale n'existe. Le message doit renvoyer vers `env:`, pas vers pip seul.
    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(ConfigError) as e:
        SecretRef._valider("keyring:svc/compte").resolve()
    assert "env:NOM" in str(e.value)


def test_en_mode_env_aucun_secret_n_est_stocke():
    # Sans trousseau, on rend une reference et on ne garde RIEN : ecrire le
    # secret « au cas ou » annulerait tout l'interet du dispositif.
    assert poser_secret("PAT_READ", "s3cr3t", trousseau=False) == "env:PAT_READ"


# ── Ne jamais perdre un fichier ─────────────────────────────────────────────

def test_un_fichier_existant_est_SAUVEGARDE(tmp_path):
    cible = tmp_path / "runner.yaml"
    cible.write_text("ancien", encoding="utf-8")
    copie = sauvegarder(cible)
    assert copie is not None and copie.read_text(encoding="utf-8") == "ancien"
    assert cible.read_text(encoding="utf-8") == "ancien", "l'original n'est pas touche"


def test_sans_fichier_existant_il_n_y_a_rien_a_sauvegarder(tmp_path):
    assert sauvegarder(tmp_path / "absent.yaml") is None


# ── Deviner les verifications ───────────────────────────────────────────────

def test_les_scripts_npm_sortent_dans_l_ordre_ou_ils_tourneront(tmp_path):
    # `run_checks` s'arrete a la premiere commande rouge : l'ordre decide de ce
    # qu'on apprend d'un echec. Le typecheck avant le build, pas l'inverse.
    (tmp_path / "package.json").write_text(
        '{"scripts": {"build": "x", "lint": "x", "typecheck": "x"}}',
        encoding="utf-8")
    assert deviner_checks(tmp_path) == [
        "npm run typecheck", "npm run lint", "npm run build"]


def test_un_pyproject_donne_ruff_et_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""
        [tool.ruff]
        line-length = 100
        [tool.pytest.ini_options]
        testpaths = ["tests"]
    """), encoding="utf-8")
    assert deviner_checks(tmp_path) == ["ruff check .", "python -m pytest -q"]


def test_un_pyproject_illisible_ne_fait_pas_tomber_l_assistant(tmp_path):
    # Un fichier casse doit donner « rien devine », pas une trace au milieu de
    # l'installation.
    (tmp_path / "pyproject.toml").write_text("[[[", encoding="utf-8")
    assert deviner_checks(tmp_path) == []


def test_un_depot_sans_rien_de_reconnaissable_ne_propose_rien(tmp_path):
    assert deviner_checks(tmp_path) == []


# ── Le clone d'installation ────────────────────────────────────────


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, f"git {' '.join(args)} :\n{r.stderr}"
    return r.stdout


def test_le_clone_d_installation_ramene_les_branches_AUTRES_que_la_defaut(tmp_path):
    """`--depth` implique `--single-branch` — mesure, pas lecture de doc.

    Un clone mono-branche n'ecrit qu'UN refspec
    (`+refs/heads/<defaut>:refs/remotes/origin/<defaut>`), et plus aucun
    `git fetch origin` ne ramenera jamais autre chose. Le demon ne peut alors
    monter la tete d'AUCUNE PR : `fatal: invalid reference: origin/dev` sur
    `frontend#406`, le 30/08/2026, dans le conteneur.

    On clone VRAIMENT un depot a deux branches. Verifier la presence du drapeau
    dans la liste d'arguments ne dirait rien de ce que git en fait — et c'est
    le comportement de git qui etait en cause, pas l'intention du code.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main", ".")

    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main", ".")
    _git(source, "config", "user.email", "test@exemple.test")
    _git(source, "config", "user.name", "Test")
    (source / "a.txt").write_text("un\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "initial")
    _git(source, "remote", "add", "origin", str(origin))
    _git(source, "push", "-q", "-u", "origin", "main")
    _git(source, "checkout", "-q", "-b", "dev")
    _git(source, "push", "-q", "-u", "origin", "dev")

    cible = tmp_path / "clone"
    r = subprocess.run(commande_de_clonage(str(origin), cible),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    assert r.returncode == 0, r.stderr

    distantes = _git(cible, "branch", "-r")
    assert "origin/main" in distantes
    assert "origin/dev" in distantes, (
        "clone mono-branche : le demon ne verra jamais la tete d'une PR"
    )
