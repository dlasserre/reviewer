"""Garde-fou : pas de caractere de controle invisible dans les sources.

Ce test existe a cause d'un bug reel. Un `\\b` de regex ecrit via un heredoc
shell a ete transforme en CARACTERE BACKSPACE (0x08). Le fichier avait l'air
juste a la relecture — le caractere ne s'affiche pas — mais le motif
`^hotfix[- ]backmerge<BS>` ne pouvait plus jamais correspondre a rien.

La panne etait doublement muette : invisible dans l'editeur, et sans effet
observable puisqu'un check non ignore qui se trouve etre vert ne change aucune
decision. Il a fallu qu'un test sur des donnees REELLES echoue pour la voir.

Un caractere de controle dans une source Python n'a jamais de raison d'etre.
Le refuser coute une milliseconde et supprime toute une classe d'erreurs
d'ecriture de fichier.
"""

from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
SOURCES = sorted((RACINE / "src").rglob("*.py")) + sorted((RACINE / "tests").rglob("*.py"))

# Tabulation, saut de ligne et retour chariot sont legitimes. Tout le reste ne
# l'est pas.
AUTORISES = {0x09, 0x0A, 0x0D}


@pytest.mark.parametrize("chemin", SOURCES, ids=lambda p: p.name)
def test_aucun_caractere_de_controle(chemin: Path):
    texte = chemin.read_text(encoding="utf-8")
    fautifs = [
        (i, c) for i, c in enumerate(texte)
        if ord(c) < 0x20 and ord(c) not in AUTORISES
    ]
    if fautifs:
        i, c = fautifs[0]
        ligne = texte.count("\n", 0, i) + 1
        pytest.fail(
            f"{chemin.name}:{ligne} contient 0x{ord(c):02X}, invisible a la "
            f"relecture ({len(fautifs)} occurrence(s)). Cause habituelle : un "
            f"antislash mange par un heredoc shell — ecrire le fichier "
            f"directement plutot que par `<<EOF`."
        )


def test_le_garde_fou_detecte_vraiment(tmp_path):
    # Un garde-fou qu'on n'a jamais vu refuser est un garde-fou dont on ne sait
    # rien.
    piege = tmp_path / "piege.py"
    piege.write_text("MOTIF = r'^hotfix\x08'\n", encoding="utf-8")
    texte = piege.read_text(encoding="utf-8")
    assert any(ord(c) < 0x20 and ord(c) not in AUTORISES for c in texte)
