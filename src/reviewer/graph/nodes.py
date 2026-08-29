"""Les etapes. Une fonction par etape, et chacune fait UNE chose.

    observe   lire la forge et l'etat local
    decide    appliquer les regles — fonction pure, aucun effet
    admit     le portier : budget du jour, puis bail
    plan      l'issue, la branche, le prompt
    code      LE CLAUDE AGENT SDK
    judge     confronter ce que l'agent dit a ce que l'arbre montre
    verify    lancer les verifications du depot
    publish   commiter, pousser, ouvrir la PR derivee
    speak     repondre dans les fils, resoudre ce qui est solde
    settle    ecrire l'etat, relacher le bail, dire le mot de la fin
    notify    la voie sans agent : prevenir quelqu'un, et s'arreter

── L'ORDRE N'EST PAS ARBITRAIRE ────────────────────────────────────────────

Le BAIL vient avant tout travail : c'est le point d'idempotence. Deux reveils
qui se chevauchent trouvent le bail pris et repartent.

Le COMMIT vient apres les VERIFICATIONS. Un commit sur un arbre dont les tests
echouent produit une PR verte en apparence et rouge en CI.

Les REPONSES viennent en DERNIER, apres le push et apres la PR. Une reponse qui
dit « corrige, voir telle PR » publiee avant que la PR existe est fausse
pendant quelques secondes — et definitivement fausse si le push echoue.

── UN SEUL CYCLE, JAMAIS DE BOUCLE ─────────────────────────────────────────

Le graphe ne boucle pas sur lui-meme. Un cycle corrige, publie, et s'arrete ;
c'est la reconciliation SUIVANTE qui redecide au vu du nouvel etat. C'est le
declenchement sur niveau, et le refaire en boucle interne le casserait : le
graphe raisonnerait sur un etat qu'il croit connaitre au lieu de le relire.

── TOUTES LES SORTIES PASSENT PAR `settle` ─────────────────────────────────

Sauf `admit` quand il refuse — la, aucun bail n'a ete pris. Partout ailleurs,
`settle` est le `finally` : il relache le bail quoi qu'il arrive. Un bail non
relache bloquerait la PR jusqu'a son expiration, et une PR bloquee sans motif
visible est exactement la signature qu'on evite.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from reviewer.agent.guard import Guard
from reviewer.agent.prompt import build_fix_prompt
from reviewer.config import Access
from reviewer.forge import actions
from reviewer.forge.base import ForgeError
from reviewer.graph.deps import Deps
from reviewer.graph.state import JobState
from reviewer.graph.sweep import avec_etat_local
from reviewer.output.events import Event
from reviewer.repo.checks import run_checks
from reviewer.repo.git import GitError, commit_all, diff_stat, push
from reviewer.repo.worktree import WorktreeError
from reviewer.rules import verdict as verdict_mod
from reviewer.rules.machine import (Action, PullSnapshot, State,
                                           compile_ignored, decide,
                                           normalise_login, severite_dominante)
from reviewer.rules.verdict import Issue

__all__ = [
    "admit", "arret_sur_exception", "code", "decider", "judge", "notify", "observe",
    "plan", "publish", "settle", "speak", "verify",
]

# Repli de nom de branche quand le modele du profil reclame le numero d'issue et
# qu'aucune issue n'a pu etre creee. Volontairement DIFFERENT du modele nominal :
# un nom degrade doit se reconnaitre au premier coup d'oeil dans `git branch`.
_DERIVEE_SANS_ISSUE = "fix/pr{pr}-revue"


def arret_sur_exception(f):
    """Une exception dans un noeud ne doit JAMAIS faire fuir le bail.

    ── CE QUE LE PORTAGE A FAILLI PERDRE ───────────────────────────────────

    Le runner d'origine tenait cette propriete par un `try/except/finally`
    autour de tout le cycle : quoi qu'il arrive, `store.release(bail)`.

    Un graphe n'a pas de `finally`. Une exception non rattrapee dans un noeud
    interrompt l'execution la ou elle est : `settle` ne tourne pas, le bail
    reste pris jusqu'a son expiration, et la PR est bloquee sans motif visible
    — exactement la signature qu'on cherche a eviter.

    On convertit donc toute exception en `stop`, que les aiguillages dirigent
    vers `settle`. Le message distingue les pannes ATTENDUES (worktree, git,
    forge) — dont le texte est deja explicite — des autres, qu'on prefixe de
    leur type parce qu'un `KeyError: 'branch'` nu n'apprend rien.

    A n'appliquer qu'aux noeuds SITUES APRES la prise du bail. Avant, il n'y a
    rien a relacher et `settle` ne doit pas tourner.
    """
    @wraps(f)
    async def enveloppe(state: JobState, deps: Deps) -> dict:
        try:
            return await f(state, deps)
        except (WorktreeError, GitError, ForgeError) as e:
            return {"stop": str(e), "reason": str(e)}
        except Exception as e:  # noqa: BLE001 — on veut l'erreur telle quelle
            raison = f"{type(e).__name__} : {e}"
            return {"stop": raison, "reason": raison}
    return enveloppe


# ── observe ─────────────────────────────────────────────────────────────────

async def observe(state: JobState, deps: Deps) -> dict:
    """Relit la verite : la forge, puis l'etat local.

    On relit meme si un balayage vient de le faire. Entre le balayage et
    l'instant ou l'on agit, une revue a pu arriver, un check tourner, la PR se
    fermer. Agir sur une photo vieille de trente secondes, c'est agir sur ce qui
    n'est plus.
    """
    repo, pr = state["repo"], state["pr"]
    pulls = await deps.reader.open_pulls(repo)
    brut = next((p for p in pulls if p.number == pr), None)
    if brut is None:
        return {"stop": "la PR n'est plus ouverte",
                "reason": f"{repo}#{pr} : fermee ou mergee depuis le balayage"}

    etat = deps.store.pull_state(state["project"], repo, pr)
    return {"snapshot": avec_etat_local(brut, deps.store, state["project"], etat),
            "pull_state": etat}


# ── decide ──────────────────────────────────────────────────────────────────

async def decider(state: JobState, deps: Deps) -> dict:
    """Applique les regles. AUCUN effet de bord — c'est tout l'interet.

    `decide` est la seule partie du systeme qui porte des regles, et la seule
    testable sans un seul stub. Le noeud ne fait que l'appeler et journaliser.
    """
    p = deps.profile
    d = decide(
        state["snapshot"],
        trusted_reviewers=frozenset(p.reviewers.trust),
        max_review_cycles=p.max_review_cycles,
        review_window_s=p.reviewers.nudge_after,
        nudge_enabled=bool(p.reviewers.nudge_comment),
        ignored_checks=compile_ignored(p.ignored_checks),
        now=datetime.now(timezone.utc),
    )

    # On ne journalise PAS les etats inertes. Un `IDLE` par PR a chaque passage
    # noierait les trois lignes qui comptent.
    if d.state not in (State.IDLE, State.WAITING_CI, State.WAITING_REVIEW):
        deps.journal.emit(Event(
            event="decide", profile=p.project, repository=state["repo"],
            pull_request=state["pr"], state=d.state.value,
            review_cycle=state["snapshot"].review_cycle, why=d.reason,
            detail={"actions": [a.value for a in d.actions],
                    "threads": [t.comment_id for t in d.threads]},
        ))
    # `next_state` et `reason` sont poses DES MAINTENANT. Quand le graphe
    # s'arrete ici — rien a faire, bail deja pris, brouillon — c'est la decision
    # qui EST le resultat. Sans ca, un cycle qui ne va pas plus loin rendrait un
    # etat final muet, et l'appelant ne saurait pas dire pourquoi.
    # Les noeuds suivants les reecrivent.
    return {"decision": d, "next_state": d.state, "reason": d.reason}


# ── admit ───────────────────────────────────────────────────────────────────

async def admit(state: JobState, deps: Deps) -> dict:
    """Le portier. Deux refus possibles, et ils ne veulent pas dire la meme chose.

    Le PLAFOND DU JOUR se verifie AVANT le bail : prendre un bail pour le
    relacher aussitot ferait clignoter la PR en `AGENT_WORKING` dans l'API
    locale, pour un job qui n'a jamais commence.

    Le BAIL est le point d'idempotence. Un reveil recu deux fois ne lance pas
    deux agents.
    """
    p = deps.profile
    plafond = p.budget.max_jobs_per_day
    deja = deps.store.jobs_today(p.project)
    if deja >= plafond:
        deps.journal.emit(Event(
            event="admit.budget_exhausted", profile=p.project,
            job_id=state["job_id"], repository=state["repo"],
            pull_request=state["pr"],
            why=f"plafond du jour atteint ({deja}/{plafond}) : "
                f"{state['repo']}#{state['pr']} reporte",
        ))
        return {"refus": f"plafond du jour atteint ({deja}/{plafond}) — reporte",
                "reason": f"plafond du jour atteint ({deja}/{plafond}) — reporte"}

    bail = deps.store.acquire(
        p.project, state["repo"], state["pr"], state["job_id"],
        ttl=timedelta(minutes=p.budget.max_minutes_per_job + 5),
    )
    if bail is None:
        return {"refus": "bail deja detenu",
                "next_state": State.AGENT_WORKING,
                "reason": f"un autre job tient deja {state['repo']}#{state['pr']}"}

    if deps.runner.writes_enabled:
        deps.store.count_job(p.project)
    return {"lease": bail}


# ── plan ────────────────────────────────────────────────────────────────────

def _derivation(snap: PullSnapshot, deps: Deps) -> tuple[bool, str]:
    """(faut-il deriver ?, socle de la derivee).

    Une PR ordinaire se travaille sur sa propre tete. Une PR dont la tete est
    PARTAGEE — release, integration — se travaille sur une derivee, qui donnera
    une PR distincte visant cette meme tete. Le worktree isole le repertoire,
    pas la reference : y commiter ecrirait sur l'integration.
    """
    tete = snap.head_ref or ""
    if tete and tete in deps.profile.shared_refs:
        return True, tete
    # La base d'une PR ordinaire est CELLE DE LA PR, pas la branche
    # d'integration du profil : un hotfix vise `main`, pas `dev`. La forge la
    # rend deja (`baseRefName`), elle n'etait simplement pas lue ici — et
    # `frontend#406` a donc cherche son socle sur `origin/dev`. Repli sur le
    # profil quand la forge ne dit rien : mieux vaut le socle habituel qu'un
    # `origin/` vide.
    return False, snap.base_ref or deps.profile.forge.integration_branch


def _nom_de_branche(snap: PullSnapshot, deps: Deps, *, derivee: bool,
                    issue: int | None) -> str:
    """Nom de la branche de travail.

    Beaucoup de depots nomment leurs branches d'apres l'issue (`fix/<n>-<slug>`),
    et c'est pour ca que l'issue est creee AVANT le worktree : un nom de branche
    ne se corrige pas apres coup sans reecrire une reference deja publiee.

    On DEGRADE le nom plutot que d'abandonner le travail quand l'issue manque —
    un correctif livre sous un nom imparfait vaut mieux qu'un correctif perdu.
    """
    if not derivee:
        return snap.head_ref or f"fix/{snap.number}"
    modele = deps.profile.derived_branch
    if "{issue}" in modele and not issue:
        modele = _DERIVEE_SANS_ISSUE
    return modele.format(pr=snap.number, issue=issue or snap.number)


@arret_sur_exception
async def plan(state: JobState, deps: Deps) -> dict:
    """De quoi lancer l'agent : l'issue de rattachement, la branche, le prompt.

    Rien n'est CREE en mode observation : une passe de lecture qui ouvre des
    issues n'est pas une passe de lecture.
    """
    p = deps.profile
    snap, etat, decision = state["snapshot"], state["pull_state"], state["decision"]
    derivee, socle = _derivation(snap, deps)

    numero_issue = None
    if deps.runner.writes_enabled and derivee:
        numero_issue = await actions.assurer_issue(
            writer=deps.writer, journal=deps.journal, store=deps.store,
            profile=p, repo=state["repo"], snap=snap,
            threads=decision.threads, etat=etat)
        if numero_issue:
            # `assurer_issue` a ECRIT en base : relire, sinon la sauvegarde de
            # fin de job reecrirait `derived_issue` a zero par-dessus.
            etat = deps.store.pull_state(p.project, state["repo"], state["pr"])

    branche = _nom_de_branche(
        snap, deps, derivee=derivee,
        issue=numero_issue or (etat.derived_issue or None))
    prompt = build_fix_prompt(
        snap, decision.threads,
        brief=None,
        review_cycle=etat.review_cycle,
        max_review_cycles=p.max_review_cycles,
        trust=frozenset(normalise_login(r) for r in p.reviewers.trust),
        work_branch=branche,
        derived_from=socle if derivee else None,
    )

    deps.journal.emit(Event(
        event="job.started", profile=p.project, job_id=state["job_id"],
        repository=state["repo"], pull_request=state["pr"],
        state=decision.state.value, review_cycle=etat.review_cycle,
        claude_session=etat.claude_session,
        trigger={"kind": "graph", "why": decision.reason,
                 "threads": [t.comment_id for t in decision.threads],
                 "branch": branche, "derived": derivee, "issue": numero_issue,
                 "untrusted_chars": prompt.untrusted_chars},
        why=decision.reason,
    ))
    return {"derived": derivee, "base_ref": socle, "issue": numero_issue,
            "branch": branche, "prompt_text": prompt.text,
            "untrusted_chars": prompt.untrusted_chars, "pull_state": etat}


# ── code ────────────────────────────────────────────────────────────────────

def _tracer(deps: Deps, state: JobState):
    """Transforme une etape d'agent en evenement, sous un nom DISTINCT.

    Un job mesure produit 60 a 120 etapes contre une dizaine de transitions.
    Melanges, les seconds disparaissent sous les premiers dans toute vue
    « derniers evenements ». Ce qui est journalise est un RESUME : le contenu
    integral des fichiers edites passerait du code source vers un journal sur
    disque, ce que personne n'a demande.
    """
    def tracer(genre: str, texte: str) -> None:
        texte = " ".join((texte or "").split())
        if not texte:
            return
        deps.journal.emit(Event(
            event="agent.step", profile=deps.profile.project,
            job_id=state["job_id"], repository=state["repo"],
            pull_request=state["pr"], state=genre,
            why=texte[:200] + ("…" if len(texte) > 200 else ""),
        ))
    return tracer


@arret_sur_exception
async def code(state: JobState, deps: Deps) -> dict:
    """LE SEUL noeud qui appelle le Claude Agent SDK.

    Le moteur suit la GRAVITE de ce qu'on corrige : un defaut de correction
    merite qu'on y mette les moyens, une coquille de nommage ne les justifie
    pas. Sans severite — un job declenche par une CI rouge, sans fil — on
    retombe sur le reglage global.
    """
    p = deps.profile
    decision, etat = state["decision"], state["pull_state"]
    repo_path = Path(state["repo_path"])

    try:
        wt = deps.worktrees.create(
            depot=repo_path, repo=state["repo"], pr=state["pr"],
            branch=state["branch"], base=state["base_ref"],
        )
    except WorktreeError as e:
        return {"stop": str(e), "reason": str(e)}

    if not wt.prepared:
        # JOURNALISE, jamais avale : un montage manque produit un echec de test
        # qui ressemble a un bug du code.
        deps.journal.emit(Event(
            event="job.links_missing", profile=p.project, job_id=state["job_id"],
            repository=state["repo"], pull_request=state["pr"],
            why=f"montage incomplet : {', '.join(wt.missing_links)}",
        ))

    contexte = tuple(r.path for r in p.repos.values()
                     if r.access is Access.CONTEXT and r.path.exists())
    guard = Guard(
        writable_root=wt.path,
        readonly_roots=tuple(r.path for r in p.repos.values()
                             if r.path != repo_path and r.path.exists()),
    )

    severite = severite_dominante(decision.threads)
    modele, effort = p.moteur(severite)
    if severite is not None:
        deps.journal.emit(Event(
            event="job.moteur", profile=p.project, repository=state["repo"],
            pull_request=state["pr"],
            why=f"severite {severite.name} -> modele {modele or 'defaut CLI'}, "
                f"effort {effort or 'defaut CLI'}",
        ))

    issue = await deps.agent(
        state["prompt_text"], worktree=wt.path, profile=p, runner=deps.runner,
        guard=guard,
        # On ne reprend la session qu'aux deux premiers cycles : au-dela, le
        # contexte accumule pese plus qu'il n'aide.
        resume=etat.claude_session if etat.review_cycle < 2 else None,
        timeout_s=p.budget.max_minutes_per_job * 60,
        output_format=verdict_mod.SCHEMA,
        extra_dirs=contexte,
        model=modele, effort=effort,
        on_step=_tracer(deps, state),
    )

    if not issue.ok:
        return {"worktree": str(wt.path), "agent": issue,
                "stop": f"l'agent n'a pas conclu : {issue.error or issue.subtype}",
                "reason": f"l'agent n'a pas conclu : {issue.error or issue.subtype}"}
    return {"worktree": str(wt.path), "agent": issue}


# ── judge ───────────────────────────────────────────────────────────────────

@arret_sur_exception
async def judge(state: JobState, deps: Deps) -> dict:
    """Confronte ce que l'agent DIT a ce que l'arbre MONTRE.

    Un cycle ou tout est `refute` ou `arbitrage` ne produit AUCUN diff, et c'est
    un resultat legitime : l'agent a lu, juge, et rendu la main. Le traiter
    comme un echec transformerait le desaccord argumente en panne.

    L'inverse — des fils declares corriges et un arbre propre — est une
    CONTRADICTION. On ne publie pas une affirmation qu'on sait fausse.
    """
    decision, agent = state["decision"], state["agent"]
    v = verdict_mod.parse(agent.structured,
                          submitted=[t.id for t in decision.threads])

    if v.anomalies:
        deps.journal.emit(Event(
            event="job.verdict_anomaly", profile=deps.profile.project,
            job_id=state["job_id"], repository=state["repo"],
            pull_request=state["pr"], why="; ".join(v.anomalies[:5]),
            detail={"count": len(v.anomalies)},
        ))
    if v.blocked:
        return {"verdict": v, "stop": f"l'agent s'est arrete : {v.blocked}",
                "reason": f"l'agent s'est arrete : {v.blocked}"}

    diff = diff_stat(Path(state["worktree"]))
    corriges = [t for t in v.threads if t.outcome is Issue.CORRIGE]
    if corriges and diff.empty:
        raison = (f"{len(corriges)} fil(s) declares corriges mais l'arbre est "
                  "propre : aucune modification a livrer.")
        return {"verdict": v, "diff": diff, "stop": raison, "reason": raison}

    return {"verdict": v, "diff": diff}


# ── verify ──────────────────────────────────────────────────────────────────

@arret_sur_exception
async def verify(state: JobState, deps: Deps) -> dict:
    """Lance les verifications du depot. Rouge = on s'arrete, sans commiter.

    La SORTIE d'un check rouge est journalisee. Sans elle, un echec ne laisse
    qu'une ligne — « ECHEC (code 2) » — et il faut rejouer la commande a la main
    pour savoir de quoi il retournait. Seulement pour les rouges : la sortie d'un
    check vert n'apprend rien et gonflerait le journal.
    """
    p = deps.profile
    rapport = run_checks(
        list(p.repos[state["repo"]].checks),
        cwd=Path(state["worktree"]),
        timeout_s=p.budget.max_minutes_per_job * 60,
        scrub_env=deps.runner.claude.scrub_env,
        on_result=lambda o: deps.journal.emit(Event(
            event="job.check", profile=p.project, job_id=state["job_id"],
            repository=state["repo"], pull_request=state["pr"], why=o.summary,
            detail=None if o.ok else {"tail": o.tail()},
        )),
    )
    if not rapport.ok:
        return {"checks": rapport,
                "stop": f"verifications rouges : {rapport.summary()}",
                "reason": f"verifications rouges : {rapport.summary()}"}
    return {"checks": rapport}


# ── publish ─────────────────────────────────────────────────────────────────

@arret_sur_exception
async def publish(state: JobState, deps: Deps) -> dict:
    """Commit, push, et la PR derivee s'il y en a une.

    Le commit porte `Refs #`, JAMAIS `Fixes #` : GitHub ferme l'issue des qu'un
    commit portant `Fixes` atteint la branche par defaut — donc au deploiement
    du code, avant l'operation que l'issue attend parfois encore (migration,
    backfill).
    """
    p = deps.profile
    wt = Path(state["worktree"])
    v, etat = state["verdict"], state["pull_state"]

    diff = commit_all(
        wt,
        v.commit_message(state["repo"], etat.review_cycle + 1,
                         issue=state.get("issue")),
        protected_refs=p.shared_refs,
    )

    jeton = state.get("write_token")
    if not jeton:
        # Ce n'est pas une panne : c'est le cran ou l'on relit avant de rendre
        # visible. Mais il doit etre DIT, sinon « rien n'a ete pousse » se lit
        # comme « rien n'a ete fait ».
        return {"diff": diff, "pushed": False, "opened_pull": None}

    push(wt, token=jeton, protected_refs=p.shared_refs)

    url = None
    if state["derived"]:
        url = await actions.ouvrir_pr(
            writer=deps.writer, journal=deps.journal, profile=p,
            repo=state["repo"], snap=state["snapshot"],
            threads=state["decision"].threads, verdict=v,
            branche=state["branch"], socle=state["base_ref"],
            issue=state.get("issue"))
    return {"diff": diff, "pushed": True, "opened_pull": url}


# ── speak ───────────────────────────────────────────────────────────────────

@arret_sur_exception
async def speak(state: JobState, deps: Deps) -> dict:
    """Repond dans les fils. EN DERNIER, une fois le travail visible."""
    repondus, resolus, demandes = await actions.publier_verdict(
        writer=deps.writer, journal=deps.journal, profile=deps.profile,
        repo=state["repo"], pr=state["pr"], threads=state["decision"].threads,
        verdict=state["verdict"],
        branche=state["branch"] if state.get("pushed") else None,
        url_pr=state.get("opened_pull"), derivee=state["derived"],
    )
    return {"replied": repondus, "resolved": resolus, "asked": demandes}


# ── notify — la voie sans agent ─────────────────────────────────────────────

async def notify(state: JobState, deps: Deps) -> dict:
    """Cycles epuises, CI bloquee : on previent, et on ne code pas.

    ── POURQUOI PAS UN `interrupt()` ───────────────────────────────────────

    Il serait tentant de suspendre le graphe ici et d'attendre la reponse. Ce
    serait faux : le signal de reprise n'arrive pas par le graphe, il arrive sur
    GITHUB — quelqu'un repond dans le fil. Un graphe suspendu attendrait une
    reprise que personne ne peut lui envoyer, en gardant un fil de checkpoint
    ouvert indefiniment.

    Le declenchement sur niveau repond deja : on previent, on s'arrete, et la
    reconciliation suivante lit la reponse et redecide. Le marqueur
    `ASK_MARK` est ce qui evite de reposer la meme question a chaque passage.

    ── DIFFERENCE ASSUMEE AVEC LE RUNNER D'ORIGINE ─────────────────────────

    La-bas, cette voie ECRIT sur la forge meme en `writes_enabled: false` : le
    garde de lecture seule est place plus loin, apres la construction du prompt,
    et cette branche sort avant de l'atteindre. Un mode annonce comme « lit,
    decide, s'arrete » publiait donc des commentaires.

    Ici le garde vient d'abord. Deux raisons :

      - « observation » doit vouloir dire observation, sans exception ;
      - les DEUX demons lisent les memes PR pendant la comparaison. Sans ce
        garde, le portage reposerait sur les fils des questions que l'original
        vient deja de poser.
    """
    decision = state["decision"]
    if not deps.runner.writes_enabled:
        deps.journal.emit(Event(
            event="notify.dry_run", profile=deps.profile.project,
            repository=state["repo"], pull_request=state["pr"],
            state=decision.state.value,
            why=f"writes_enabled: false — personne n'est prevenu. "
                f"Aurait dit : {decision.reason}",
        ))
        return {"asked": 0, "next_state": decision.state,
                "reason": f"{decision.reason} (lecture seule : personne prevenu)"}

    pose = await actions.prevenir_humain(
        writer=deps.writer, journal=deps.journal, store=deps.store,
        profile=deps.profile, repo=state["repo"], pr=state["pr"],
        snap=state["snapshot"], decision=decision, etat=state["pull_state"],
    )
    return {"asked": pose, "next_state": decision.state,
            "reason": decision.reason if pose else
            f"{decision.reason} (deja signale, rien de nouveau a dire)"}


# ── settle ──────────────────────────────────────────────────────────────────

async def settle(state: JobState, deps: Deps) -> dict:
    """Le mot de la fin : l'etat en base, le bail rendu, la raison dite.

    C'est le `finally` du graphe. Toutes les voies y passent — sauf un refus
    d'admission, ou aucun bail n'a ete pris.

    ── UN ARRET CONSOMME UN CYCLE ──────────────────────────────────────────

    Reussite comme echec, `review_cycle` augmente. Sans cela, un job qui echoue
    laisse l'etat inchange, donc la meme decision au passage suivant, donc le
    meme echec — indefiniment, jusqu'a epuiser le plafond du jour. Un echec
    coute un cycle : c'est ce qui fait converger la boucle vers `NEEDS_HUMAN`
    au lieu de la faire tourner.
    """
    p = deps.profile
    etat, agent = state["pull_state"], state.get("agent")
    arret = state.get("stop")

    deps.store.save_pull_state(replace(
        etat,
        claude_session=(agent.session_id if agent else None) or etat.claude_session,
        worktree=state.get("worktree") or etat.worktree,
        review_cycle=etat.review_cycle + 1,
        last_handled_comment_id=(
            etat.last_handled_comment_id if arret else
            max([etat.last_handled_comment_id,
                 *(t.last_seen_id for t in state["decision"].threads)])),
    ))

    if arret:
        prevenu = await actions.signaler_arret(
            writer=deps.writer, journal=deps.journal, store=deps.store,
            profile=p, repo=state["repo"], pr=state["pr"],
            snap=state["snapshot"], etat=etat, raison=arret,
            branche=state.get("branch"), checks=state.get("checks"),
            anomalies=state["verdict"].anomalies if state.get("verdict") else (),
            fils=state["decision"].threads,
        )
        deps.journal.emit(Event(
            event="job.needs_human", profile=p.project, job_id=state["job_id"],
            repository=state["repo"], pull_request=state["pr"],
            state=State.NEEDS_HUMAN.value,
            claude_session=agent.session_id if agent else None,
            worktree=state.get("worktree"), result="needs_human", why=arret,
            detail={"notified": prevenu, "branch": state.get("branch")},
        ))
        _rendre_le_bail(state, deps)
        return {"next_state": State.NEEDS_HUMAN, "reason": arret,
                "asked": 1 if prevenu else 0}

    # ── L'etat qui suit dit CE QU'ON ATTEND, et trois choses different ──────
    #
    #   quelqu'un   un fil reste ouvert sur un desaccord. Prime sur tout le
    #               reste : meme si du code est parti en CI, la PR ne bougera
    #               pas sans decision.
    #   la CI       le correctif est pousse, les checks distants vont tourner.
    #   un regard   il y a un commit, mais aucun jeton pour le pousser. Annoncer
    #               `WAITING_CI` serait faux, aucune CI ne va se declencher.
    #
    # `en_attente` se lit dans le VERDICT, pas dans ce qui a ete publie : sans
    # jeton rien n'est publie, et compter les publications ferait passer une PR
    # suspendue a un arbitrage pour une PR terminee.
    v = state["verdict"]
    en_attente = [t for t in v.threads if t.outcome.needs_human]
    corriges = [t for t in v.threads if t.outcome is Issue.CORRIGE]
    diff, rapport = state.get("diff"), state.get("checks")

    if en_attente:
        suite = State.NEEDS_HUMAN
    elif state.get("pushed"):
        suite = State.WAITING_CI
    else:
        suite = State.READY_FOR_HUMAN

    resume = (
        (diff.summary() if diff else "aucun diff")
        + (f" — {rapport.summary()}" if rapport else " — aucun test a relancer")
        + f" — {len(corriges)} corrige(s), {len(en_attente)} en attente d'arbitrage"
        + ("" if state.get("pushed") else " — non pousse (aucun jeton d'ecriture)")
    )
    deps.journal.emit(Event(
        event="job.finished", profile=p.project, job_id=state["job_id"],
        repository=state["repo"], pull_request=state["pr"], state=suite.value,
        review_cycle=etat.review_cycle + 1,
        claude_session=agent.session_id if agent else None,
        worktree=state.get("worktree"),
        result="pousse" if state.get("pushed") else (
            "commite" if diff and not diff.empty else "sans diff"),
        why=resume,
        detail={"cost_usd": agent.cost_usd if agent else None,
                "branch": state.get("branch"), "derived": state["derived"],
                "pull": state.get("opened_pull"), "replied": state["replied"],
                "resolved": state["resolved"], "asked": state["asked"]},
    ))
    _rendre_le_bail(state, deps)
    return {"next_state": suite, "reason": resume}


def _rendre_le_bail(state: JobState, deps: Deps) -> None:
    """Toujours, quoi qu'il arrive.

    Un bail non relache bloquerait la PR jusqu'a son expiration, et une PR
    bloquee sans motif visible est exactement la signature qu'on evite.
    """
    if bail := state.get("lease"):
        deps.store.release(bail)
