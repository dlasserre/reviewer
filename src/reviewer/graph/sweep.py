"""Quel travail y a-t-il, et dans quel ordre le mener ?

Le balayage regarde TOUS les depots d'un profil et rend, pour chaque PR
ouverte, ce qu'il faudrait y faire. Puis `ordonnancer` decoupe ce travail en
vagues menees de front.

── POURQUOI LE BALAYAGE DECIDE, ALORS QUE LE GRAPHE DECIDE AUSSI ───────────

Ce n'est pas une redite : les deux decisions ne servent pas a la meme chose et
ne sont pas prises au meme instant.

Le balayage decide pour ORDONNANCER. Il lui faut savoir quelles PR demandent un
agent, lesquelles ont une tete partagee, sur quels depots — sans quoi il ne peut
ni repartir les vagues ni respecter le plafond du jour.

Le graphe redecide pour AGIR, au moment ou il agit. Entre les deux, il s'est
ecoule le temps des vagues precedentes — jusqu'a `max_minutes_per_job` par job.
Une PR jugee `NEEDS_FIX` il y a vingt-cinq minutes a pu etre mergee, fermee, ou
recevoir une revue depuis.

Le runner d'origine agissait sur la photo du balayage. Ici, le graphe relit.
Cout : une requete GraphQL par job. Face a un job qui dure des minutes et
consomme du quota, c'est gratuit.

── LE BALAYAGE N'ECRIT RIEN ────────────────────────────────────────────────

Aucune ecriture sur la forge, aucun bail, aucun cycle consomme. Il lit et il
range. C'est ce qui permet de le lancer a tout moment — `status` n'est que sa
sortie formatee.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from fnmatch import fnmatch

from reviewer.config import Access, ProfileConfig
from reviewer.output.events import Event, Journal
from reviewer.rules.machine import (BRANCHES_PARTAGEES, Action, Decision,
                                           PullSnapshot, State, compile_ignored,
                                           decide, normalise_login)
from reviewer.store.leases import PullState, StateStore

__all__ = [
    "Outcome",
    "SweepReport",
    "avec_etat_local",
    "ordonnancer",
    "sweep_profile",
]


@dataclass(frozen=True, slots=True)
class Outcome:
    """Ce qu'on a decide pour une PR, et pourquoi."""

    repo: str
    number: int
    decision: Decision
    snapshot: PullSnapshot

    @property
    def actionable(self) -> bool:
        """Cette PR demande-t-elle un AGENT ?

        Distinct de « il y a quelque chose a faire » : une PR dont les cycles
        sont epuises n'a rien a coder, mais elle a quelqu'un a appeler.
        """
        return Action.RUN_AGENT in self.decision.actions


@dataclass(frozen=True, slots=True)
class SweepReport:
    """Bilan d'un passage. RENDU MEME A VIDE, et c'est le point.

    Sans ligne de bilan, « rien a faire » et « le demon ne tourne plus »
    produisent exactement le meme journal. Le compteur ne prouve pas que la
    decision est juste — il prouve que le passage a eu lieu.
    """

    profile: str
    outcomes: tuple[Outcome, ...] = ()
    broken: tuple[tuple[str, str], ...] = ()   # (depot, raison)
    skipped: tuple[tuple[str, str], ...] = ()  # (depot, raison)

    @property
    def by_state(self) -> dict[State, int]:
        out: dict[State, int] = {}
        for o in self.outcomes:
            out[o.decision.state] = out.get(o.decision.state, 0) + 1
        return out

    def summary(self) -> str:
        etats = ", ".join(f"{s.value}={n}" for s, n in sorted(
            self.by_state.items(), key=lambda kv: kv[0].value)) or "aucune PR"
        bilan = f"[{self.profile}] {len(self.outcomes)} PR : {etats}"
        if self.skipped:
            bilan += f" — {len(self.skipped)} depot(s) hors perimetre"
        if self.broken:
            bilan += f" — ECHEC sur {', '.join(d for d, _ in self.broken)}"
        return bilan


def avec_etat_local(pull: PullSnapshot, store: StateStore, projet: str,
                    etat: PullState | None = None) -> PullSnapshot:
    """Complete la photo de la forge avec ce que le demon sait, lui.

    QUATRE champs de `PullSnapshot` ne viennent PAS de la forge — elle ne les
    connait pas :

        last_handled_comment_id   sinon toute remarque parait non traitee
        review_cycle              sinon la borne de cycles ne se declenche jamais
        lease_held
        nudge_sent

    Le premier est le plus couteux : le job ECRIT bien son curseur en base a la
    fin de chaque cycle, mais si personne ne le relit, l'agent reprend les memes
    remarques a chaque passage, indefiniment, en consommant le quota partage —
    et `max_review_cycles`, la borne censee arreter ca, ne peut pas se
    declencher puisque le compteur repart de zero.

    La panne est invisible en lecture seule : `status` affiche NEEDS_FIX, ce qui
    est exactement ce qu'on attend d'une PR avec des fils ouverts. Elle ne se
    manifeste qu'une fois le demon arme, sur le quota.
    """
    etat = etat if etat is not None else store.pull_state(projet, pull.repo, pull.number)
    bail = store.lease(projet, pull.repo, pull.number)
    return replace(
        pull,
        lease_held=bail is not None and not store.reclaimable(bail),
        review_cycle=etat.review_cycle,
        last_handled_comment_id=etat.last_handled_comment_id,
        nudge_sent=etat.nudge_sent,
    )


def _auteur_dans_le_perimetre(auteur: str, logins: list[str]) -> bool:
    """Cette PR appartient-elle a ce demon ?

    Liste VIDE = tout le monde. C'est le cas d'un demon unique, partage par une
    equipe : lui faire tout refuser transformerait une absence d'avis en
    interdiction totale.

    La comparaison passe par `normalise_login` : la forme GraphQL d'un compte ne
    reconnait pas toujours sa forme REST, et un perimetre qui ne reconnait
    personne est un demon qui ne fait rien — sans que rien ne dise pourquoi.
    """
    if not logins:
        return True
    connus = {normalise_login(x) for x in logins}
    return normalise_login(auteur) in connus


def _branche_autorisee(branche: str, motifs: list[str]) -> bool:
    """La branche de tete correspond-elle a un motif du profil ?

    Une liste VIDE autorise tout : c'est le cas d'un profil qui ne se prononce
    pas, et lui faire tout refuser transformerait une absence d'avis en
    interdiction totale — l'agent ne ferait plus rien, sans que rien ne dise
    pourquoi.

    Une branche INCONNUE passe. C'est deliberé : « je ne connais pas la
    branche » n'est pas « elle ne correspond pas ». La refuser ici produirait un
    saut silencieux, alors que la creation du worktree echoue quelques etapes
    plus loin en disant exactement ce qui manque.
    """
    if not motifs or not branche:
        return True
    return any(fnmatch(branche, m) for m in motifs)


async def sweep_profile(
    profile: ProfileConfig,
    reader,
    journal: Journal,
    store: StateStore,
    *,
    forces: "set[tuple[str, int]] | None" = None,
    now: datetime | None = None,
) -> SweepReport:
    """Un passage complet sur un profil.

    Les depots sont traites INDEPENDAMMENT : l'echec de l'un ne prive pas les
    autres de leur passage. Le Worker de livraison a appris ca a ses depens — un
    balayage depot par depot epuisait son plafond de sous-requetes dans le
    premier et n'atteignait jamais les suivants.

    `store` est OBLIGATOIRE, et positionnel. Le rendre facultatif laisserait
    revenir en silence le defaut que `avec_etat_local` repare : un appelant qui
    l'oublie retomberait sur des curseurs a zero, donc sur un agent qui refait
    le meme travail sans fin. Un parametre requis ne s'oublie pas.
    """
    now = now or datetime.now(timezone.utc)
    outcomes: list[Outcome] = []
    broken: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []

    from reviewer.forge.base import ForgeError  # noqa: PLC0415 — cycle

    trust = frozenset(profile.reviewers.trust)
    ignores = compile_ignored(profile.ignored_checks)

    for nom, repo in profile.repos.items():
        # Seuls les depots en ECRITURE produisent du travail. Un depot
        # `context` est lu par l'agent comme contexte, jamais reconcilie : lui
        # chercher du travail n'aurait aucun sens puisqu'on ne peut rien y
        # faire, et le journaliser comme « rien a faire » laisserait croire
        # qu'on s'en occupe.
        if repo.access is not Access.WRITE:
            skipped.append((nom, f"acces={repo.access.value}"))
            continue

        try:
            pulls = await reader.open_pulls(nom)
        except ForgeError as e:
            broken.append((nom, str(e)))
            journal.emit(Event(
                event="sweep.repo_failed", profile=profile.project,
                repository=nom, why=str(e),
            ))
            continue

        for brut in pulls:
            # Le perimetre AVANT tout le reste : une PR hors perimetre n'est pas
            # « rien a faire », elle n'appartient pas a ce demon. La compter dans
            # le bilan ferait croire qu'on s'en occupe.
            if not _auteur_dans_le_perimetre(brut.author, profile.scope.authors):
                continue
            pull = avec_etat_local(brut, store, profile.project)

            # La liste `branches` du profil dit sur quelles branches l'agent a
            # le droit de travailler. C'est une POLITIQUE de projet, et elle
            # reste ici plutot que dans `decide` : le moteur porte les regles
            # universelles (une branche partagee ne se travaille jamais), le
            # profil porte ce que CE projet autorise. Melanger les deux rendrait
            # le moteur dependant d'une configuration.
            #
            # Une branche PARTAGEE passe volontairement ce filtre : `decide` la
            # traitera juste apres, en la nommant pour ce qu'elle est (« PR de
            # livraison ») plutot que par ce qu'elle n'est pas.
            if (pull.head_ref not in BRANCHES_PARTAGEES
                    and not _branche_autorisee(pull.head_ref, repo.branches)):
                outcomes.append(Outcome(nom, pull.number, Decision(
                    State.IDLE,
                    f"branche « {pull.head_ref} » hors des motifs autorises "
                    f"pour {nom} ({', '.join(repo.branches)}).",
                ), pull))
                continue

            d = decide(
                pull,
                trusted_reviewers=trust,
                max_review_cycles=profile.max_review_cycles,
                review_window_s=profile.reviewers.nudge_after,
                nudge_enabled=bool(profile.reviewers.nudge_comment),
                ignored_checks=ignores,
                # Un forcage demande depuis la console. `sweep_profile` ne le
                # CONSOMME pas : c'est l'appelant qui tient le registre, et lui
                # seul sait si le job a vraiment demarre.
                forced=(nom, pull.number) in (forces or set()),
                now=now,
            )
            outcomes.append(Outcome(nom, pull.number, d, pull))

            # On ne journalise PAS les etats inertes. Un `IDLE` par PR a chaque
            # passage noierait les trois lignes qui comptent — et le bilan de
            # fin dit deja que le passage a eu lieu.
            if d.state in (State.IDLE, State.WAITING_CI, State.WAITING_REVIEW):
                continue
            journal.emit(Event(
                event="sweep.decision", profile=profile.project,
                repository=nom, pull_request=pull.number,
                state=d.state.value, review_cycle=pull.review_cycle, why=d.reason,
                detail={"actions": [a.value for a in d.actions],
                        "threads": [t.comment_id for t in d.threads]},
            ))

    rapport = SweepReport(profile.project, tuple(outcomes), tuple(broken),
                          tuple(skipped))
    journal.emit(Event(
        event="sweep.done", profile=profile.project, why=rapport.summary(),
        detail={"states": {s.value: n for s, n in rapport.by_state.items()}},
    ))
    return rapport


def ordonnancer(
    outcomes: list[Outcome],
    *,
    max_parallel: int,
    shared_refs: frozenset[str] | set[str],
    restant: int,
) -> tuple[list[list[Outcome]], list[Outcome]]:
    """Repartit le travail en VAGUES menees de front, et dit ce qui est reporte.

    Trois regles, et chacune vient d'une contrainte reelle.

    1. UN SEUL JOB PAR DEPOT A LA FOIS. Deux jobs d'un meme depot partagent son
       `.git` : `fetch`, `worktree add` et `worktree remove` y ecrivent tous.
       Les mener de front, c'est courir apres une corruption d'index pour gagner
       quelques minutes. Deux DEPOTS differents, en revanche, n'ont rien en
       commun — c'est la que le parallelisme est gratuit, et c'est le cas
       courant ici : une PR par depot.

    2. UNE PR DE LIVRAISON PASSE SEULE. Sa tete est une branche partagee, donc
       le job DERIVE une branche et ouvre une PR distincte. Ce chemin touche
       plus de choses qu'un correctif ordinaire — une issue, une branche neuve,
       une PR — et il vaut mieux le regarder sans rien d'autre autour.

    3. LE PLAFOND `restant` PORTE SUR LES JOBS QUI CODENT. Une PR qui n'a qu'une
       question a poser ne consomme ni agent ni quota ; la compter dans la
       limite ferait taire des arbitrages pour rien.
    """
    vagues: list[list[Outcome]] = []
    reportes: list[Outcome] = []
    en_cours: list[Outcome] = []
    depots_de_la_vague: set[str] = set()
    budget = restant

    def fermer() -> None:
        nonlocal en_cours, depots_de_la_vague
        if en_cours:
            vagues.append(en_cours)
            en_cours, depots_de_la_vague = [], set()

    for o in outcomes:
        if o.actionable:
            if budget <= 0:
                reportes.append(o)
                continue
            budget -= 1

        # Regle 2 : seule dans sa vague, avant comme apres.
        if o.snapshot.head_ref in shared_refs:
            fermer()
            vagues.append([o])
            continue

        # Regle 1 : un depot deja pris dans cette vague la ferme.
        if o.repo in depots_de_la_vague or len(en_cours) >= max_parallel:
            fermer()
        en_cours.append(o)
        depots_de_la_vague.add(o.repo)

    fermer()
    return vagues, reportes
