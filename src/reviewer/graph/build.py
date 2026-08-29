"""Le cablage. Qui suit qui, et sur quelle condition.

── LE PLAN ─────────────────────────────────────────────────────────────────

    observe ──> decider ──┬── rien a faire ─────────────────────────> FIN
                          ├── prevenir ────> notify ────────────────> FIN
                          └── travailler ──> admit ──┬── refuse ────> FIN
                                                     └── admis ──> plan
                                                                    │
        ┌───────────────────────────────────────────────────────────┘
        ├── echec ─────────────────────────────────────────────> settle
        ├── observation seule ──> dry_run ─────────────────────────> FIN
        └── coder ──> code ──┬── arret ──────────────────────> settle
                             └── juger ──> judge ──┬── arret ─> settle
                                                   ├── sans diff ──> speak
                                                   └── verifier ──> verify
                                                                      │
                    ┌─────────────────────────────────────────────────┘
                    ├── rouge ────────────────────────────────> settle
                    └── vert ──> publish ──┬── echec ──> settle
                                           └── speak ──> settle ──> FIN

── DEUX SORTIES SANS `settle`, ET C'EST VOULU ──────────────────────────────

`observe` qui ne trouve plus la PR, et `admit` qui refuse : dans les deux cas
AUCUN BAIL n'a ete pris. Passer par `settle` y consommerait un cycle de revue
pour un travail qui n'a jamais commence, et relacherait un bail appartenant a
quelqu'un d'autre.

`dry_run` non plus : le mode observation ne consomme pas de cycle et ne previent
personne. Il rend juste le bail qu'il a pris.

── LE FIL DE CHECKPOINT EST LE JOB, PAS LA PR ──────────────────────────────

`thread_id = job_id`. Un fil par PR ferait reprendre le cycle suivant la ou le
precedent s'etait arrete — donc raisonner sur un etat qu'on croit connaitre au
lieu de le relire. Chaque cycle est un fil neuf.

Le job_id d'un cycle interrompu n'est pas perdu : il est ECRIT DANS LE BAIL.
`store.sweep_dead()` rend les baux dont le processus est mort, avec leur
`job_id` — c'est par la qu'on reprend.
"""

from __future__ import annotations

from functools import partial, wraps

from langgraph.graph import END, START, StateGraph

from reviewer.graph import nodes
from reviewer.graph.deps import Deps
from reviewer.graph.state import JobState, etat_initial
from reviewer.output.events import Event
from reviewer.rules.machine import Action

__all__ = ["construire", "lancer_job", "topologie"]


# ── Les aiguillages. Chacun rend le NOM du noeud suivant. ────────────────────

def _apres_observe(state: JobState) -> str:
    """La PR existe-t-elle encore ?"""
    return END if state.get("stop") else "decider"


def _apres_decide(state: JobState) -> str:
    """Trois issues, dans l'ordre de ce qu'elles coutent."""
    actions = state["decision"].actions
    if Action.RUN_AGENT in actions:
        return "admit"
    if Action.ASK_HUMAN in actions:
        return "notify"
    return END


def _apres_admit(state: JobState) -> str:
    return END if state.get("refus") else "plan"


def _apres_plan(deps: Deps):
    """Mode observation : on va jusqu'au prompt et on s'arrete la.

    C'est la meme sequence que le Worker de livraison — observer d'abord, armer
    ensuite — et elle a l'avantage de rendre le prompt inspectable avant qu'il
    ne serve.
    """
    def aiguiller(state: JobState) -> str:
        if state.get("stop"):
            return "settle"
        return "code" if deps.runner.writes_enabled else "dry_run"
    return aiguiller


def _apres_code(state: JobState) -> str:
    return "settle" if state.get("stop") else "judge"


def _apres_judge(state: JobState) -> str:
    """Un cycle sans diff n'est PAS un echec.

    Tout refute ou tout en arbitrage : l'agent a lu, juge, rendu la main. Il n'y
    a rien a verifier ni a pousser, mais il y a tout a DIRE — d'ou le saut
    direct vers `speak`.
    """
    if state.get("stop"):
        return "settle"
    diff = state.get("diff")
    return "speak" if (diff is None or diff.empty) else "verify"


def _apres_verify(state: JobState) -> str:
    return "settle" if state.get("stop") else "publish"


def _apres_publish(state: JobState) -> str:
    """Un push refuse ne doit PAS enchainer sur `speak`.

    Les reponses annonceraient « corrige, voir telle branche » pour un
    correctif qui n'est jamais parti. On s'arrete, et `settle` le dit.
    """
    return "settle" if state.get("stop") else "speak"


# ── Le montage ───────────────────────────────────────────────────────────────

def construire(deps: Deps, *, checkpointer=None):
    """Monte le graphe, avec `deps` capture par fermeture.

    Pourquoi pas `config["configurable"]` : la fermeture garde le type visible.
    Un `deps` passe par la configuration ferait de chaque acces un `dict.get`
    dont l'erreur n'apparait qu'a l'execution, dans le noeud, apres l'appel au
    SDK — donc au moment le plus cher.
    """
    def lie(nom: str, f):
        """Attache `deps` au noeud, et ANNONCE son entree.

        ── POURQUOI L'ENTREE, ET PAS LA SORTIE ─────────────────────────────

        Un evenement de sortie n'arrive qu'une fois le noeud fini. Or le noeud
        le plus long est `code` — jusqu'a trente minutes d'agent. Une console
        qui n'afficherait que les sorties resterait figee sur l'etape
        precedente pendant tout ce temps, et donnerait a voir un demon bloque
        la ou il travaille.

        L'entree du noeud suivant vaut sortie du precedent : la sequence est
        lineaire, un seul noeud tourne a la fois par job. Un evenement suffit
        donc a dire ou en est le job, et il coute douze lignes de journal par
        cycle.

        Le journal peut etre absent (montage a vide, pour inspecter la
        topologie). Une trace manquante ne doit jamais empecher un job de
        tourner : l'observation est un confort, le travail ne l'est pas.
        """
        @wraps(f)
        async def enveloppe(state: JobState) -> dict:
            if deps.journal is not None:
                deps.journal.emit(Event(
                    event="graph.node", profile=deps.profile.project,
                    job_id=state.get("job_id"), repository=state.get("repo"),
                    pull_request=state.get("pr"), state=nom,
                ))
            return await f(state, deps=deps)
        return enveloppe

    g = StateGraph(JobState)
    g.add_node("observe", lie("observe", nodes.observe))
    g.add_node("decider", lie("decider", nodes.decider))
    g.add_node("admit", lie("admit", nodes.admit))
    g.add_node("plan", lie("plan", nodes.plan))
    g.add_node("dry_run", lie("dry_run", _dry_run))
    g.add_node("code", lie("code", nodes.code))
    g.add_node("judge", lie("judge", nodes.judge))
    g.add_node("verify", lie("verify", nodes.verify))
    g.add_node("publish", lie("publish", nodes.publish))
    g.add_node("speak", lie("speak", nodes.speak))
    g.add_node("notify", lie("notify", nodes.notify))
    g.add_node("settle", lie("settle", nodes.settle))

    g.add_edge(START, "observe")
    g.add_conditional_edges("observe", _apres_observe, ["decider", END])
    g.add_conditional_edges("decider", _apres_decide, ["admit", "notify", END])
    g.add_conditional_edges("admit", _apres_admit, ["plan", END])
    g.add_conditional_edges("plan", _apres_plan(deps), ["code", "dry_run", "settle"])
    g.add_conditional_edges("code", _apres_code, ["judge", "settle"])
    g.add_conditional_edges("judge", _apres_judge, ["verify", "speak", "settle"])
    g.add_conditional_edges("verify", _apres_verify, ["publish", "settle"])
    g.add_conditional_edges("publish", _apres_publish, ["speak", "settle"])
    g.add_edge("speak", "settle")
    g.add_edge("notify", END)
    g.add_edge("dry_run", END)
    g.add_edge("settle", END)

    return g.compile(checkpointer=checkpointer)


async def _dry_run(state: JobState, deps: Deps) -> dict:
    """Observation : le prompt est construit, l'agent n'est pas lance.

    Le bail est rendu tout de suite. Aucun cycle consomme, personne prevenu :
    une passe de lecture ne doit rien laisser derriere elle.
    """
    from reviewer.output.events import Event  # noqa: PLC0415

    nodes._rendre_le_bail(state, deps)
    deps.journal.emit(Event(
        event="job.dry_run", profile=deps.profile.project, job_id=state["job_id"],
        repository=state["repo"], pull_request=state["pr"], result="dry-run",
        why="writes_enabled: false — prompt construit, agent non lance",
        detail={"prompt_chars": len(state["prompt_text"]),
                "untrusted_chars": state["untrusted_chars"],
                "branch": state["branch"], "derived": state["derived"]},
    ))
    return {"dry_run": True, "next_state": state["decision"].state,
            "reason": "lecture seule : prompt construit, agent non lance"}


async def lancer_job(
    graphe,
    *,
    project: str,
    repo: str,
    pr: int,
    job_id: str,
    repo_path: str,
    write_token: str | None = None,
) -> JobState:
    """Un cycle complet sur une PR. Rend l'etat final.

    `recursion_limit` est genereux : le graphe ne boucle pas, sa profondeur est
    bornee par construction. La limite ne sert qu'a rattraper un cablage faux —
    et un cablage faux doit lever, pas tourner.
    """
    depart = etat_initial(project=project, repo=repo, pr=pr, job_id=job_id,
                          repo_path=repo_path, write_token=write_token)
    return await graphe.ainvoke(
        depart,
        config={"configurable": {"thread_id": job_id}, "recursion_limit": 25},
    )


# ── La topologie, pour qui veut la DESSINER ─────────────────────────────────

# Ce que fait chaque noeud, en une ligne. Ici et pas dans la console : un schema
# dessine a la main derive du cablage des la premiere modification, et personne
# ne s'en apercoit — un dessin faux ne leve rien.
#
# COURT, et c'est une contrainte de dessin, pas un choix de style : ce texte est
# rendu DANS la boite du noeud, large de 128 px. Au-dela de 24 caracteres il
# deborde, et un libelle qui deborde salit exactement ce qu'on essaie de rendre
# lisible. L'explication longue vit dans la docstring du noeud.
LIBELLES = {
    "observe": ("Observer", "la forge, l'etat local"),
    "decider": ("Decider", "les regles, sans effet"),
    "admit": ("Admettre", "budget, puis bail"),
    "plan": ("Planifier", "issue, branche, prompt"),
    "dry_run": ("Lecture seule", "prompt seul"),
    "code": ("Coder", "le Claude Agent SDK"),
    "judge": ("Juger", "verdict contre arbre"),
    "verify": ("Verifier", "les checks du depot"),
    "publish": ("Publier", "commit, push, PR"),
    "speak": ("Repondre", "repondre, resoudre"),
    "notify": ("Prevenir", "la voie sans agent"),
    "settle": ("Conclure", "etat, bail, raison"),
}


def topologie() -> dict:
    """Le graphe tel qu'il est CABLE — noeuds et arcs, lus du graphe compile.

    ── POURQUOI NE PAS ECRIRE CETTE LISTE A LA MAIN ────────────────────────

    Parce qu'un schema ecrit a la main se met a mentir au premier arc ajoute, et
    que rien ne le signale : un dessin faux ne leve aucune erreur, il se
    contente d'etre faux. En le lisant du graphe compile, la console ne peut
    afficher que ce qui tourne reellement.

    Le montage a vide est sans danger : `construire` ne fait qu'attacher des
    fonctions et declarer des arcs. Aucun `Deps` n'est TOUCHE avant qu'un noeud
    tourne, et ici aucun ne tourne. Seul l'aiguillage qui suit `plan` lit
    `runner.writes_enabled`, et lui non plus n'est appele au montage.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    vide = Deps(
        runner=SimpleNamespace(writes_enabled=True),
        profile=SimpleNamespace(project="-"),
        store=None, journal=None, worktrees=None, reader=None,
    )
    g = construire(vide).get_graph()
    return {
        "nodes": [
            {"id": n,
             "label": LIBELLES.get(n, (n, ""))[0],
             "detail": LIBELLES.get(n, (n, ""))[1]}
            for n in g.nodes if n not in ("__start__", "__end__")
        ],
        "edges": [
            {"source": e.source, "target": e.target,
             "conditional": bool(e.conditional)}
            for e in g.edges
        ],
    }
