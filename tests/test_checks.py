"""Le lanceur de verifications dit-il la verite sur ce qui a tourne ?

Le piege de ce module n'est pas de mal lancer une commande : c'est de rendre un
verdict qui a l'air complet alors qu'il ne l'est pas. Un « 1 echec » qui cache
« 3 non lancees » se lit exactement comme une couverture complete.
"""

from __future__ import annotations

import sys

import pytest

from agent_runner_lg.repo.checks import (CheckOutcome, CheckReport, outils_locaux,
                                 run_checks)

# Un interpreteur qu'on sait present, plutot qu'un `echo` dont le comportement
# differe entre cmd, PowerShell et bash.
PY = f'"{sys.executable}"'
VERT = f'{PY} -c "print(\'ok\')"'
ROUGE = f'{PY} -c "import sys; print(\'raté\'); sys.exit(1)"'
LENT = f'{PY} -c "import time; time.sleep(30)"'


def test_aucune_commande_n_est_pas_un_succes(tmp_path):
    # « Rien a verifier » ne doit pas se lire comme « tout est vert » : c'est
    # une configuration a completer, pas un feu vert.
    r = run_checks([], cwd=tmp_path)
    assert not r.ok
    assert "aucune verification configuree" in r.summary()


def test_tout_vert(tmp_path):
    r = run_checks([VERT, VERT], cwd=tmp_path)
    assert r.ok
    assert r.failure is None
    assert len(r.outcomes) == 2
    assert "2/2" in r.summary()


def test_on_s_arrete_a_la_premiere_rouge(tmp_path):
    r = run_checks([VERT, ROUGE, VERT, VERT], cwd=tmp_path)
    assert not r.ok
    assert len(r.outcomes) == 2          # la 3e et la 4e n'ont pas tourne
    assert r.failure.command == ROUGE
    assert r.failure.returncode == 1


def test_les_commandes_non_lancees_sont_RENDUES(tmp_path):
    # C'est le point du module. Les taire ferait passer une couverture
    # partielle pour une couverture complete.
    r = run_checks([ROUGE, VERT, VERT], cwd=tmp_path)
    assert len(r.not_run) == 2
    assert "2 non lancee(s)" in r.summary()


def test_des_commandes_non_lancees_rendent_le_rapport_rouge(tmp_path):
    # Meme si tout ce qui a tourne est vert : un verdict rendu sur une
    # couverture partielle est un verdict faux.
    r = CheckReport(outcomes=run_checks([VERT], cwd=tmp_path).outcomes,
                    not_run=("npm run build",))
    assert not r.ok


def test_la_sortie_est_capturee(tmp_path):
    r = run_checks([ROUGE], cwd=tmp_path)
    assert "raté" in r.outcomes[0].output


def test_une_sortie_enorme_garde_la_tete_ET_la_queue(tmp_path):
    # Selon l'outil l'information utile est a un bout ou a l'autre : pytest
    # resume a la fin, eslint enumere au fil.
    cmd = (f'{PY} -c "print(\'DEBUT\'); '
           f'print(\'x\' * 200000); print(\'FIN\')"')
    r = run_checks([cmd], cwd=tmp_path)
    sortie = r.outcomes[0].output
    assert "DEBUT" in sortie
    assert "FIN" in sortie
    assert "caracteres coupes" in sortie
    assert len(sortie) < 20000


def test_un_timeout_est_distingue_d_un_echec(tmp_path):
    # « Le code ne finit pas » et « le code est faux » ne se corrigent pas
    # pareil : il faut pouvoir les distinguer dans le journal.
    r = run_checks([LENT], cwd=tmp_path, timeout_s=1.0)
    assert not r.ok
    o = r.outcomes[0]
    assert o.timed_out
    assert o.returncode is None
    assert "TIMEOUT" in o.summary


def test_une_commande_introuvable_est_signalee_sans_lever(tmp_path):
    r = run_checks(["binaire-qui-nexiste-vraiment-pas --version"], cwd=tmp_path)
    assert not r.ok
    # Le shell rend un code non nul plutot qu'une OSError : dans les deux cas,
    # le rapport doit le dire au lieu de faire tomber le job.
    assert r.outcomes[0].returncode != 0 or r.outcomes[0].error


def test_les_variables_sensibles_sont_retirees(tmp_path, monkeypatch):
    # `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` exige bubblewrap et n'existe pas sous
    # Windows : on nettoie nous-memes ce qu'on lance directement.
    monkeypatch.setenv("SECRET_A_NE_PAS_FUITER", "valeur")
    cmd = f'{PY} -c "import os; print(os.environ.get(\'SECRET_A_NE_PAS_FUITER\', \'ABSENTE\'))"'

    fuite = run_checks([cmd], cwd=tmp_path)
    assert "valeur" in fuite.outcomes[0].output      # sans nettoyage, elle passe

    propre = run_checks([cmd], cwd=tmp_path, scrub_env=("SECRET_A_NE_PAS_FUITER",))
    assert "ABSENTE" in propre.outcomes[0].output


def test_la_sortie_est_stabilisee(tmp_path):
    # Couleurs et barres de progression rendent un journal illisible.
    cmd = f'{PY} -c "import os; print(os.environ.get(\'NO_COLOR\'), os.environ.get(\'CI\'))"'
    r = run_checks([cmd], cwd=tmp_path)
    assert "1 1" in r.outcomes[0].output


def test_les_commandes_tournent_dans_le_worktree(tmp_path):
    (tmp_path / "temoin.txt").write_text("ici", encoding="utf-8")
    cmd = f'{PY} -c "import pathlib; print(pathlib.Path(\'temoin.txt\').read_text())"'
    r = run_checks([cmd], cwd=tmp_path)
    assert "ici" in r.outcomes[0].output


def test_un_repertoire_absent_leve_tot(tmp_path):
    # Mieux vaut lever ici qu'obtenir N echecs incomprehensibles.
    with pytest.raises(FileNotFoundError):
        run_checks([VERT], cwd=tmp_path / "nulle-part")


def test_les_outils_du_depot_sont_mis_dans_le_PATH(tmp_path):
    # Un profil ecrit `ruff check .` — la forme que documentent les conventions
    # du depot, et celle qu'un humain tape. Elle suppose le venv ACTIVE : sans
    # cette preparation, la commande echoue avec « n'est pas reconnu », ce qui
    # ressemble a un depot casse alors que c'est l'environnement qui manque.
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    binaires = tmp_path / "node_modules" / ".bin"
    binaires.mkdir(parents=True)

    trouves = [p.name for p in outils_locaux(tmp_path)]
    assert "Scripts" in trouves
    assert ".bin" in trouves


def test_un_depot_sans_venv_ni_node_modules_ne_gene_pas(tmp_path):
    assert outils_locaux(tmp_path) == []


def test_le_venv_du_depot_prime_dans_le_PATH(tmp_path):
    # Un faux « outil » dans le venv du depot doit etre trouve avant celui du
    # systeme : c'est ce que fait une activation.
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    faux = scripts / ("outil-local.cmd" if sys.platform == "win32" else "outil-local")
    if sys.platform == "win32":
        faux.write_text("@echo VENV DU DEPOT\n", encoding="utf-8")
    else:
        faux.write_text("#!/bin/sh\necho 'VENV DU DEPOT'\n", encoding="utf-8")
        faux.chmod(0o755)

    r = run_checks(["outil-local"], cwd=tmp_path)
    assert r.ok, r.summary()
    assert "VENV DU DEPOT" in r.outcomes[0].output


def test_virtual_env_est_pose(tmp_path):
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    cmd = f'{PY} -c "import os; print(os.environ.get(\'VIRTUAL_ENV\', \'ABSENT\'))"'
    r = run_checks([cmd], cwd=tmp_path)
    assert ".venv" in r.outcomes[0].output


def test_les_accents_de_la_sortie_survivent(tmp_path):
    # Les 4890 tests du backend portent des noms francais : une sortie
    # illisible est un diagnostic perdu.
    cmd = f'{PY} -c "print(\'échec de la vérification : requalifié\')"'
    r = run_checks([cmd], cwd=tmp_path)
    assert "échec de la vérification : requalifié" in r.outcomes[0].output


def test_l_avancement_est_rapporte_au_fil(tmp_path):
    # Sans ca, le journal montre un silence de plusieurs minutes suivi d'un
    # verdict — et on ne sait pas si le demon travaille ou s'il est bloque.
    vus = []
    run_checks([VERT, VERT, ROUGE, VERT], cwd=tmp_path, on_result=vus.append)
    assert [o.ok for o in vus] == [True, True, False]


# ── La sortie d'un check rouge doit etre RECUPERABLE ────────────────────────


def test_la_fin_de_la_sortie_est_rendue():
    # Mesure du 29/08/2026 : un `pytest` sorti en code 2 n'a produit qu'une
    # ligne exploitable — « ECHEC (code 2) ». Il a fallu rejouer la commande a
    # la main pour decouvrir que le worktree n'avait pas de `.env`, que la
    # collecte echouait sur 336 fichiers, et que le code livre n'y etait pour
    # rien.
    o = CheckOutcome(command="pytest -q", ok=False, returncode=2, duration_s=14.3,
                     output="\n".join(f"ligne {i}" for i in range(60)))
    fin = o.tail(lignes=5)
    assert fin.splitlines() == [f"ligne {i}" for i in range(55, 60)]


def test_la_fin_est_coupee_par_la_GAUCHE():
    # La derniere ligne est celle qui conclut : couper par la droite retirerait
    # precisement le verdict qu'on cherche.
    o = CheckOutcome(command="x", ok=False, returncode=1, duration_s=1.0,
                     output="a" * 400 + "\nINTERROMPU : 336 erreurs")
    fin = o.tail(lignes=5, max_chars=60)
    assert fin.endswith("INTERROMPU : 336 erreurs")
    assert fin.startswith("…")


def test_une_sortie_vide_ne_rend_rien():
    # Sans ca, le commentaire publierait un bloc de code vide, qui se lit
    # comme « la commande n'a rien dit » alors qu'elle n'a pas ete capturee.
    o = CheckOutcome(command="x", ok=False, returncode=1, duration_s=1.0, output="   \n ")
    assert o.tail() == ""
