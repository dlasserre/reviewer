"""Le journal rend-il ce qu'il a ecrit ?

Ce module manquait, et le defaut qu'il aurait attrape a coute une soiree :
`recent()` ne lisait que la memoire du processus. Un demon qui redemarre
repartait donc d'un journal vide — la console affichait « journal vide » alors
que le fichier du jour portait tout, et le fil d'un job termine avant le
redemarrage etait definitivement invisible depuis l'API.

Le journal n'est pas un decor : c'est la piste d'audit du demon, le seul endroit
ou l'on peut relire pourquoi un agent a travaille. Il merite des tests qui
regardent le FICHIER, pas seulement l'objet.
"""

from __future__ import annotations

from reviewer.output.events import Event, Journal


def test_ce_qui_est_emis_est_rendu(tmp_path):
    j = Journal(tmp_path, profile="p")
    j.emit(Event(event="job.started", profile="p", why="on y va"))
    assert [e.why for e in j.recent(50)] == ["on y va"]


def test_recent_relit_le_FICHIER_quand_la_memoire_est_courte(tmp_path):
    # Le cas du redemarrage : un processus NEUF ne porte rien en memoire, et
    # doit pourtant retrouver ce que le precedent a ecrit.
    ancien = Journal(tmp_path, profile="p")
    for i in range(4):
        ancien.emit(Event(event="agent.step", profile="p", repository="backend",
                          pull_request=727, state="outil", why=f"Read f{i}.py"))

    neuf = Journal(tmp_path, profile="p")
    relus = neuf.recent(50)

    assert [e.why for e in relus] == [f"Read f{i}.py" for i in range(4)]
    # Et les champs de filtrage survivent au passage par le disque : sans eux,
    # le fil d'un job ne pourrait plus etre isole d'un autre.
    assert all(e.repository == "backend" and e.pull_request == 727 for e in relus)


def test_le_fil_relu_ne_DOUBLE_pas_ce_que_la_memoire_porte(tmp_path):
    # Le meme evenement est dans le fichier ET en memoire. S'il apparaissait
    # deux fois, on lirait deux actions la ou il n'y en a eu qu'une.
    j = Journal(tmp_path, profile="p")
    j.emit(Event(event="job.started", profile="p", repository="backend",
                 pull_request=727, why="on y va"))
    assert sum(1 for e in j.recent(50) if e.why == "on y va") == 1


def test_le_fil_est_rendu_dans_l_ORDRE_CHRONOLOGIQUE(tmp_path):
    # Concatener « fichier puis memoire » placerait un evenement ecrit a
    # l'instant par un autre processus AVANT un evenement plus ancien de
    # celui-ci. Un fil qui remonte le temps est pire qu'un fil incomplet : on
    # le lit sans se mefier.
    autre = Journal(tmp_path, profile="p")
    autre.emit(Event(event="a", profile="p", ts="2026-08-29T10:00:00Z", why="plus tard"))

    ici = Journal(tmp_path, profile="p")
    ici.emit(Event(event="b", profile="p", ts="2026-08-29T09:00:00Z", why="plus tot"))

    assert [e.why for e in ici.recent(50)] == ["plus tot", "plus tard"]


def test_une_ligne_ILLISIBLE_ne_prive_pas_des_autres(tmp_path):
    # Ecriture coupee par un arret brutal, format d'une version anterieure :
    # une ligne perdue ne doit pas emporter tout le journal du jour.
    j = Journal(tmp_path, profile="p")
    j.emit(Event(event="a", profile="p", why="valide"))
    fichier = next(tmp_path.glob("*.jsonl"))
    fichier.write_text(fichier.read_text(encoding="utf-8") + "{pas du json\n",
                       encoding="utf-8")

    assert [e.why for e in Journal(tmp_path, profile="p").recent(50)] == ["valide"]


def test_un_journal_INACCESSIBLE_ne_fait_pas_tomber_le_demon(tmp_path):
    # On perd la trace, jamais le travail. Le bus, lui, recoit quand meme.
    j = Journal(tmp_path / "absent" / "encore", profile="p")
    (tmp_path / "absent" / "encore").rmdir()
    (tmp_path / "absent").rmdir()
    j.emit(Event(event="a", profile="p", why="quand meme"))
    assert [e.why for e in j.recent(50)] == ["quand meme"]
