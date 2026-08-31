"""Outillage de la copie locale — la panne `code 127` de `backend#748`.

Le 30/08/2026, un job s'est arrete sur « verifications rouges : arret sur :
ruff check . — ECHEC (code 127) ». Ni `ruff` ni `pytest` n'existaient dans le
conteneur, et le message accusait le code livre.

Ces tests tiennent les quatre proprietes qui suppriment cette confusion :
l'outillage tourne, il ne se rejoue pas pour rien, il ne DETRUIT rien, et son
echec est bruyant.
"""

from __future__ import annotations

import sys
from pathlib import Path

from reviewer.bootstrap import deviner_setup
from reviewer.repo.checks import CheckOutcome
from reviewer.repo.provision import EMPREINTE, assurer_outillage


def test_les_commandes_tournent_et_l_empreinte_est_deposee(tmp_path: Path):
    assurer_outillage(tmp_path, [f'{sys.executable} -c "open(\'temoin\',\'w\').close()"'])
    assert (tmp_path / "temoin").is_file()
    assert (tmp_path / EMPREINTE).is_file()


def test_un_second_passage_ne_rejoue_rien(tmp_path: Path):
    """Sinon chaque job reinstallerait tout — des minutes, a chaque fois."""
    commande = f'{sys.executable} -c "open(\'compteur\',\'a\').write(\'x\')"'
    assurer_outillage(tmp_path, [commande])
    resultat = assurer_outillage(tmp_path, [commande])

    assert (tmp_path / "compteur").read_text() == "x"   # UNE seule execution
    assert resultat.joue is False
    assert resultat.summary() == "outillage deja en place"


def test_une_declaration_modifiee_rejoue(tmp_path: Path):
    """L'empreinte porte sur ce qui est DECLARE : editer le profil doit relancer."""
    ecrire = "open('compteur','a').write('x')"
    assurer_outillage(tmp_path, [f'{sys.executable} -c "{ecrire}"'])
    assurer_outillage(tmp_path, [f'{sys.executable} -c "{ecrire}"', "echo autre"])
    assert (tmp_path / "compteur").read_text() == "xx"


def test_un_echec_est_rendu_et_l_empreinte_n_est_PAS_deposee(tmp_path: Path):
    """Une copie a moitie outillee doit rejouer, pas se croire prete."""
    resultat = assurer_outillage(tmp_path, [f"{sys.executable} -c \"raise SystemExit(3)\""])

    assert not resultat.ok
    assert len(resultat.echouees) == 1
    assert "ECHEC" in resultat.summary()
    assert not (tmp_path / EMPREINTE).exists()


def test_on_s_arrete_a_la_premiere_commande_rouge(tmp_path: Path):
    """Enchainer sur une base cassee n'apprend rien et coute des minutes."""
    resultat = assurer_outillage(tmp_path, [
        f'{sys.executable} -c "raise SystemExit(1)"',
        f'{sys.executable} -c "open(\'jamais\',\'w\').close()"',
    ])
    assert not resultat.ok
    assert not (tmp_path / "jamais").exists()


# ── Fichiers d'environnement ────────────────────────────────────────────────

def test_un_fichier_d_environnement_manquant_est_ecrit(tmp_path: Path):
    """Un clone nu n'a que ce que git suit — or les tests lisent ces variables
    au chargement, donc leur absence casse la COLLECTE, pas un test."""
    assurer_outillage(tmp_path, [], env_files={".env": {"APP_ENV": "prod",
                                                       "MONGO_URL": "mongodb://localhost:1"}})
    corps = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "APP_ENV=prod" in corps
    assert "MONGO_URL=mongodb://localhost:1" in corps


def test_un_fichier_d_environnement_EXISTANT_n_est_JAMAIS_ecrase(tmp_path: Path):
    """Sur un poste de developpement, la copie locale porte le VRAI `.env`.

    L'ecraser par une recette de test couperait la machine de son
    environnement — et ce module n'a aucun moyen de savoir lequel il regarde.
    """
    (tmp_path / ".env").write_text("MONGO_URL=le-vrai\n", encoding="utf-8")
    resultat = assurer_outillage(tmp_path, [], env_files={".env": {"MONGO_URL": "bidon"}})

    assert (tmp_path / ".env").read_text(encoding="utf-8") == "MONGO_URL=le-vrai\n"
    assert resultat.fichiers == ()


# ── Ce qu'on devine ─────────────────────────────────────────────────────────

def test_deviner_setup_python_passe_par_un_venv_du_depot(tmp_path: Path):
    """NON-REGRESSION `backend#748`.

    Sans venv, `pip install` vise le `site-packages` du systeme — que
    l'utilisateur du conteneur (uid 1000) ne peut pas ecrire. L'installation
    echouait sans rien arreter, et le premier job mourait en « code 127 ».
    """
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (tmp_path / "requirements-dev.txt").write_text("ruff\n", encoding="utf-8")

    setup = deviner_setup(tmp_path)

    assert setup[0] == "python -m venv .venv"          # le venv D'ABORD
    assert "pip install --no-cache-dir -r requirements.txt" in setup
    assert "pip install --no-cache-dir -r requirements-dev.txt" in setup
    # `pip` NON qualifie : le chemin du venv n'est pas le meme selon la
    # plateforme, et l'environnement est recalcule avant chaque commande.
    assert not any(".venv/bin/pip" in c or ".venv\\Scripts" in c for c in setup)


def test_deviner_setup_npm_prefere_ci_quand_le_verrou_existe(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    assert deviner_setup(tmp_path) == ["npm ci --no-audit --no-fund"]


def test_deviner_setup_ne_devine_rien_sur_un_depot_nu(tmp_path: Path):
    assert deviner_setup(tmp_path) == []


# ── Le message ──────────────────────────────────────────────────────────────

def test_le_code_127_est_nomme_et_pas_confondu_avec_un_lint_rouge():
    """C'est le message qui a coute le plus cher : « ECHEC (code 127) » se lit
    comme une verification rouge et envoie chercher un bug dans le code."""
    absent = CheckOutcome("ruff check .", False, 127, 0.0)
    rouge = CheckOutcome("ruff check .", False, 1, 2.5)

    assert absent.outil_absent
    assert "OUTIL INTROUVABLE" in absent.summary
    assert "n'est pas outille" in absent.summary

    assert not rouge.outil_absent
    assert "OUTIL INTROUVABLE" not in rouge.summary
