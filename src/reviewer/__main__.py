"""Ligne de commande.

Quatre commandes, et elles forment une progression :

    init     assistant d'installation en terminal (la console fait pareil)
    check    la configuration est-elle valide, et surtout OPERANTE ?
    status   que ferait le demon, la, maintenant ? (aucune ecriture)
    run      un passage : balayage, puis le graphe sur chaque PR retenue
    serve    le demon : `run` toutes les `reconcile_every`, plus l'API locale

`writes_enabled` reste a `false` tant que `status` du portage n'affiche pas les
memes decisions que le runner d'origine sur les memes PR. Observer d'abord,
armer ensuite : c'est la sequence qui a fonctionne pour le Worker de livraison,
puis pour le runner lui-meme.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from reviewer.agent.trust import ensure_trusted, untrusted
from reviewer.config import (EFFORTS, Access, ConfigError, MoteurConfig,
                                    ProfileConfig, RunnerConfig, load_profiles,
                                    load_runner)
from reviewer.forge.reader import GitHubReader
from reviewer.forge.writer import GitHubWriter
from reviewer.graph.build import construire, lancer_job
from reviewer.graph.deps import Deps
from reviewer.graph.sweep import (Outcome, SweepReport, ordonnancer,
                                         sweep_profile)
from reviewer.output.events import Event, Journal, new_job_id
from reviewer.repo.worktree import WorktreeManager, segments_caches
from reviewer.rules.machine import (Action, Severity, State,
                                           compile_ignored)
from reviewer.store.leases import StateStore

# Sous Windows la console est en cp1252 : sans cela, la moindre fleche fait
# echouer l'ecriture, donc toute la sortie. `stderr` AUSSI — c'est la que
# partent les messages d'erreur, et un message d'erreur illisible est pire
# qu'une absence de message.
for _flux in (sys.stdout, sys.stderr):
    try:
        _flux.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_PASTILLE = {
    State.IDLE: "  ",
    State.AGENT_WORKING: "**",
    State.WAITING_CI: "..",
    State.WAITING_REVIEW: "..",
    State.NEEDS_FIX: "->",
    State.READY_FOR_HUMAN: "OK",
    State.NEEDS_HUMAN: "!!",
}


def _servir_installation(chemin: Path, port: int = 8788) -> int:
    """Sans configuration, on sert la page qui permet d'en faire une.

    ── POURQUOI PAS UN ECHEC ───────────────────────────────────────────────

    Un demon qui s'arrete en disant « configuration absente » oblige a savoir,
    avant meme de l'avoir vu tourner, quel fichier ecrire et avec quoi dedans.
    Personne ne lance six commandes pour essayer un outil.

    Il demarre donc, et sert la seule chose utile a ce moment-la : de quoi se
    configurer. La console normale, elle, n'a aucune route d'ecriture — le
    chemin de code qui ecrit n'existe QUE tant qu'il n'y a rien a proteger.
    """
    import uvicorn  # noqa: PLC0415

    from reviewer.bootstrap import en_conteneur  # noqa: PLC0415
    from reviewer.output.setup import create_setup_app  # noqa: PLC0415

    # En conteneur la frontiere est le namespace reseau, et la publication cote
    # hote borne l'exposition. Sur un poste, la boucle locale suffit.
    hote = "0.0.0.0" if en_conteneur() else "127.0.0.1"  # noqa: S104
    print(f"Aucune configuration en {chemin}.")
    print(f"Ouvrir http://127.0.0.1:{port} pour installer.")
    print("Le demon n'a aucun depot, aucun jeton et aucun droit tant que ce")
    print("n'est pas fait.")
    try:
        uvicorn.run(create_setup_app(chemin, port=port), host=hote, port=port,
                    log_level="warning")
    except KeyboardInterrupt:
        pass
    return 0


def _charger(runner_path: Path) -> tuple[RunnerConfig, dict[str, ProfileConfig]]:
    # Les secrets ecrits par la console vivent a cote de la configuration. Ils
    # sont charges AVANT elle : c'est eux que ses references `env:NOM` lisent.
    from reviewer.output.setup import charger_secrets  # noqa: PLC0415

    charger_secrets(runner_path.parent)
    runner = load_runner(runner_path)
    dossier = runner.profiles_dir
    if not dossier.is_absolute():
        dossier = (runner_path.parent / dossier).resolve()
    profils, erreurs = load_profiles(dossier)
    for fichier, raison in erreurs.items():
        # Un profil ecarte est DIT. L'avaler ferait passer « ce projet est mal
        # configure » pour « ce projet n'a rien a faire ».
        print(f"[profil ecarte] {fichier}\n{raison}\n", file=sys.stderr)
    return runner, profils


def _token(profile: ProfileConfig) -> str:
    if profile.forge.token_read is not None:
        return profile.forge.token_read.resolve()
    # Repli explicite : `gh` a deja un jeton, et exiger une variable de plus
    # pour une commande en lecture seule ferait renoncer a l'essayer.
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        if val := os.environ.get(var):
            return val
    raise ConfigError(
        f"aucun jeton de lecture pour « {profile.project} » : poser "
        "`forge.token_read: env:...` dans le profil, ou GH_TOKEN dans "
        "l'environnement."
    )


def _token_ecriture(profile: ProfileConfig) -> str | None:
    """Jeton d'ecriture, ou `None` — auquel cas on commite sans pousser.

    L'absence n'est PAS une erreur : commiter dans le worktree sans pousser est
    un mode utile, celui ou l'on veut relire le travail avant qu'il devienne
    visible. Mais elle est DITE, sinon « rien n'a ete pousse » se lit comme
    « rien n'a ete fait ».
    """
    if profile.forge.token_write is None:
        return None
    try:
        return profile.forge.token_write.resolve()
    except ConfigError as e:
        print(f"[{profile.project}] pas de jeton d'ecriture : {e}", file=sys.stderr)
        return None


def _chemin_checkpoints(runner: RunnerConfig) -> Path:
    """La base des points de reprise, a cote de l'etat local.

    SEPAREE de `state.db`, volontairement. Les deux ne se purgent pas au meme
    rythme : les baux et les curseurs sont durables, un checkpoint n'a de valeur
    que tant que son job peut reprendre. Les melanger ferait hesiter a nettoyer
    les seconds de peur de perdre les premiers.
    """
    return runner.state_db.parent / "checkpoints.db"


# ── status ──────────────────────────────────────────────────────────────────

async def _status(runner: RunnerConfig, profils: dict[str, ProfileConfig],
                  only: str | None) -> int:
    """Ce que le demon ferait, sans rien faire.

    OUVRE l'etat local, en lecture. Sans lui, le balayage deciderait sur des
    curseurs a zero — donc afficherait « fils ouverts » sur des remarques deja
    traitees, et ne montrerait jamais qu'un cycle a ete consomme. Le tableau
    qu'on lit pour armer le demon doit etre celui que le demon voit, pas une
    variante optimiste.
    """
    journal = Journal(runner.logs_dir)
    store = StateStore(runner.state_db)
    rapports: list[SweepReport] = []

    try:
        for nom, profile in profils.items():
            if only and nom != only:
                continue
            ecrivables = profile.repos_by_access(Access.WRITE)
            contexte = profile.repos_by_access(Access.CONTEXT)
            print(f"\n=== {nom} — {len(ecrivables)} depot(s) en ecriture, "
                  f"{len(contexte)} en contexte")
            if not ecrivables:
                print("    (aucun depot en ecriture : rien a balayer)")
                continue

            async with GitHubReader(
                    profile.forge.org, _token(profile),
                    ignored_checks=compile_ignored(profile.ignored_checks)) as reader:
                rapport = await sweep_profile(profile, reader, journal, store)
            rapports.append(rapport)

            for o in sorted(rapport.outcomes, key=lambda o: (o.repo, o.number)):
                print(f"  {_PASTILLE[o.decision.state]} {o.repo}#{o.number:<5} "
                      f"{o.decision.state.value:<16} {o.decision.reason}")
                if o.snapshot.review_cycle:
                    print(f"        cycle {o.snapshot.review_cycle}/"
                          f"{profile.max_review_cycles}, curseur de remarques "
                          f"a {o.snapshot.last_handled_comment_id}")
            for depot, raison in rapport.skipped:
                print(f"     -- {depot} : {raison}")
            for depot, raison in rapport.broken:
                print(f"  !! {depot} : {raison}")
            # Une ligne de bilan a CHAQUE passage, meme a vide : sans elle,
            # « rien a faire » et « le demon ne tourne plus » se lisent pareil.
            print(f"  {rapport.summary()}")
    finally:
        store.close()

    if runner.writes_enabled:
        print("\n/!\\ writes_enabled: true — le demon est arme.", file=sys.stderr)
    else:
        print("\nLecture seule (writes_enabled: false). Aucune ecriture emise.")
    return 1 if any(r.broken for r in rapports) else 0


# ── run ─────────────────────────────────────────────────────────────────────

def _lignes_du_job(o: Outcome, final: dict, profile: ProfileConfig) -> list[str]:
    """Le bloc de sortie d'un job, RENDU et non imprime.

    Rendre les lignes plutot que de les ecrire est ce qui permet de mener
    plusieurs jobs de front : imprimees au fil de l'eau, elles s'entrelaceraient
    et on ne saurait plus quelle ligne appartient a quelle PR.
    """
    etat = final.get("next_state") or o.decision.state
    lignes = [f"  -> {o.repo}#{o.number} : {o.decision.reason}"]
    marque = {State.WAITING_CI: "OK", State.NEEDS_HUMAN: "!!"}.get(etat, "  ")
    lignes.append(f"     {marque} {etat.value} — "
                  f"{final.get('reason') or o.decision.reason}")

    if final.get("branch") and final.get("derived"):
        lignes.append(f"        branche derivee : {final['branch']} "
                      "(la tete de la PR est partagee)")
    if final.get("issue"):
        lignes.append(f"        issue de rattachement : #{final['issue']}")
    elif final.get("derived") and profile.issues.enabled:
        # Le DIRE : une PR derivee sans issue a l'air normale, et le
        # rattachement manquant ne se voit qu'a la lecture du corps.
        lignes.append("        !! aucune issue de rattachement — "
                      "a lier a la main (voir le journal)")
    if final.get("opened_pull"):
        lignes.append(f"        PR du correctif : {final['opened_pull']}")
    if final.get("replied") or final.get("resolved") or final.get("asked"):
        lignes.append(f"        fils : {final.get('replied', 0)} reponse(s), "
                      f"{final.get('resolved', 0)} resolu(s), "
                      f"{final.get('asked', 0)} en attente d'arbitrage")
    if final.get("worktree"):
        lignes.append(f"        worktree : {final['worktree']}")
    if (agent := final.get("agent")) is not None and agent.cost_usd:
        lignes.append(f"        cout : {agent.cost_usd:.4f}")
    return lignes


async def _traiter(graphe, o: Outcome, *, profile: ProfileConfig,
                   jeton: str | None) -> list[str]:
    """Un cycle de graphe sur une PR, et son bloc de sortie."""
    final = await lancer_job(
        graphe,
        project=profile.project, repo=o.repo, pr=o.number,
        job_id=new_job_id(), repo_path=str(profile.repos[o.repo].path),
        write_token=jeton,
    )
    return _lignes_du_job(o, final, profile)


def _reprendre_les_baux(store: StateStore, journal: Journal) -> None:
    """Au demarrage : la table des baux decrit un monde qui n'existe plus.

    Apres un reboot, les processus qui les tenaient ont disparu. On les reprend,
    et on les JOURNALISE — un bail nettoye en silence ferait disparaitre la
    trace d'un job interrompu, or c'est justement le `job_id` de ce bail qui
    permettrait de reprendre son graphe la ou il s'est arrete.
    """
    for bail in store.sweep_dead():
        journal.emit(Event(
            event="lease.reclaimed", profile=bail.profile, job_id=bail.job_id,
            repository=bail.repo, pull_request=bail.pr,
            why=f"bail repris au demarrage (pid {bail.pid} absent ou bail expire)",
        ))


def _assurer_confiance(runner: RunnerConfig, nom: str, ecrivables: dict,
                       journal: Journal) -> None:
    """Sans confiance, Claude Code ignore le `.claude/settings.json` des depots.

    Leurs hooks compris : le depot perdrait ses garde-fous a l'instant ou
    l'agent y ecrit.

    Rien n'est ecrit si tout est deja fiable — Claude Code tient ce meme fichier
    pendant qu'une session tourne, et chaque ecriture inutile est une occasion
    de perdre ce qu'il vient d'y mettre.
    """
    if not (runner.writes_enabled and runner.claude.trust_workspaces
            and runner.claude.config_dir is not None):
        return
    rapport = ensure_trusted(
        runner.claude.config_dir,
        [d.path for d in ecrivables.values() if d.path.exists()])
    for chemin in rapport.granted:
        print(f"    [confiance] {chemin} — reglages du depot desormais "
              "appliques (hooks compris)")
        journal.emit(Event(
            event="trust.granted", profile=nom,
            why=f"{chemin} marque fiable dans la configuration du demon"))
    for quoi, pourquoi in rapport.failed:
        # JAMAIS avale : un depot non fiable travaille sans ses hooks, et rien
        # d'autre ne le dira.
        print(f"    !! confiance non posee sur {quoi} : {pourquoi}",
              file=sys.stderr)
        journal.emit(Event(event="trust.failed", profile=nom,
                           why=f"{quoi} : {pourquoi}"))


async def _run(runner: RunnerConfig, profils: dict[str, ProfileConfig],
               only: str | None, limit: int) -> int:
    """Balaye, puis fait tourner le graphe sur le travail identifie.

    La borne `limit` est volontairement BASSE par defaut. Un passage qui lance
    dix jobs d'un coup rend illisible ce qui s'est passe, et le quota est
    partage avec l'usage humain.
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: PLC0415

    store = StateStore(runner.state_db)
    journal = Journal(runner.logs_dir)
    _reprendre_les_baux(store, journal)

    lances = 0
    try:
        # UN SEUL checkpointer pour tout le passage. Il porte les points de
        # reprise de chaque job, indexes par `job_id` : deux jobs concurrents
        # n'ont donc rien a partager, et un job interrompu se retrouve par le
        # `job_id` que porte son bail.
        async with AsyncSqliteSaver.from_conn_string(
                str(_chemin_checkpoints(runner))) as checkpointer:
            for nom, profile in profils.items():
                if only and nom != only:
                    continue
                ecrivables = profile.repos_by_access(Access.WRITE)
                if not ecrivables:
                    continue

                # AVANT tout job, et une seule fois par passage.
                _assurer_confiance(runner, nom, ecrivables, journal)

                async with GitHubReader(
                        profile.forge.org, _token(profile),
                        ignored_checks=compile_ignored(profile.ignored_checks)
                ) as reader:
                    rapport = await sweep_profile(profile, reader, journal, store)
                    print(f"\n=== {nom} — {rapport.summary()}")

                    jeton = _token_ecriture(profile)
                    if jeton is None and runner.writes_enabled:
                        print("    (pas de jeton d'ecriture : les correctifs "
                              "seront commites dans le worktree, sans push, et "
                              "PERSONNE ne sera prevenu sur la forge)")

                    # Le writer n'existe QUE s'il y a un jeton. Une capacite
                    # absente est une garantie plus solide qu'une capacite
                    # presente qu'on s'engage a ne pas appeler — c'est la meme
                    # raison qui fait qu'il y a deux jetons plutot qu'un.
                    writer = GitHubWriter(profile.forge.org, jeton) if jeton else None
                    try:
                        if writer is not None:
                            await writer.__aenter__()

                        deps = Deps(
                            runner=runner, profile=profile, store=store,
                            journal=journal,
                            worktrees=WorktreeManager(
                                root=runner.worktrees_root, profile=nom,
                                # Les branches protegees viennent du PROFIL :
                                # une liste ecrite pour `dev` ne protege pas un
                                # projet dont l'integration s'appelle autrement.
                                protected_refs=profile.shared_refs),
                            reader=reader, writer=writer,
                        )
                        graphe = construire(deps, checkpointer=checkpointer)

                        a_traiter = [
                            o for o in rapport.outcomes
                            # `actionable` ne suffit pas : une PR dont les
                            # cycles sont epuises n'a rien a coder, mais elle a
                            # quelqu'un a appeler. L'ignorer la laissait muette.
                            if o.actionable or Action.ASK_HUMAN in o.decision.actions
                        ]
                        vagues, reportes = ordonnancer(
                            a_traiter, max_parallel=runner.max_parallel,
                            shared_refs=profile.shared_refs,
                            restant=max(0, limit - lances))
                        for o in reportes:
                            # Une coupe qui ne se journalise pas se lit comme
                            # une couverture complete.
                            print(f"    -- {o.repo}#{o.number} : reporte "
                                  f"(limite de {limit} job(s) atteinte)")

                        for vague in vagues:
                            if len(vague) > 1:
                                print(f"  ~~ {len(vague)} jobs menes de front : "
                                      + ", ".join(f"{o.repo}#{o.number}"
                                                  for o in vague))
                            blocs = await asyncio.gather(*[
                                _traiter(graphe, o, profile=profile, jeton=jeton)
                                for o in vague
                            ])
                            for lignes in blocs:
                                print("\n".join(lignes))
                            lances += sum(1 for o in vague if o.actionable)
                    finally:
                        if writer is not None:
                            await writer.__aexit__(None, None, None)
    finally:
        store.close()

    if not runner.writes_enabled:
        print("\nLecture seule (writes_enabled: false) : prompts construits, "
              "agent non lance.")
    print(f"\n{lances} job(s) traite(s).")
    return 0


class Reveil:
    """Ce qui interrompt l'attente entre deux passages.

    La boucle attend `reconcile_every` OU ce signal, le premier des deux. Un
    `sleep` nu ne peut pas etre interrompu : il faudrait attendre la fin du
    delai pour qu'un reveil soit pris en compte, et un bouton qui agit dans
    quatre minutes n'est pas un bouton.
    """

    def __init__(self) -> None:
        self._signal = asyncio.Event()
        self.en_cours = False
        self.dernier: str | None = None

    def demander(self) -> bool:
        """Rend False si un passage tourne deja — deux se disputeraient les baux."""
        if self.en_cours:
            return False
        self._signal.set()
        return True

    async def attendre(self, delai: float) -> bool:
        """Attend le delai, ou le signal. Rend True si c'est le signal."""
        try:
            await asyncio.wait_for(self._signal.wait(), timeout=delai)
        except asyncio.TimeoutError:
            return False
        self._signal.clear()
        return True


# ── serve ───────────────────────────────────────────────────────────────────

async def _boucle_de_travail(runner: RunnerConfig,
                             profils: dict[str, ProfileConfig],
                             limit: int, reveil: "Reveil | None" = None) -> None:
    """Balaye et traite, indefiniment, toutes les `reconcile_every`.

    Le modele DECLENCHE SUR NIVEAU rend cette boucle suffisante a elle seule :
    on ne reagit pas a un evenement, on relit l'etat et on en deduit le travail.
    Une livraison webhook perdue — GitHub ne les rejoue pas — se rattrape au
    passage suivant. Le reveil par long-poll ne fera qu'y gagner de la latence.

    UNE ERREUR N'ARRETE PAS LA BOUCLE. Un jeton expire, GitHub indisponible, une
    panne reseau : le passage suivant reessaiera. Un demon qui meurt sur la
    premiere erreur transitoire est pire qu'absent — on le croit vivant.
    """
    intervalle = runner.wake.reconcile_every
    while True:
        if reveil is not None:
            reveil.en_cours = True
        try:
            await _run(runner, profils, None, limit)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # Jamais avale : sans cette ligne, un demon qui echoue a chaque
            # passage est indiscernable d'un demon qui n'a rien a faire.
            print(f"!! passage en echec : {type(e).__name__} : {e}",
                  file=sys.stderr)
        finally:
            if reveil is not None:
                reveil.en_cours = False

        if reveil is None:
            await asyncio.sleep(intervalle)
        elif await reveil.attendre(intervalle):
            print("Balayage demande depuis la console.")


def _serve(runner: RunnerConfig, profils: dict[str, ProfileConfig],
           limit: int, *, travailler: bool = True) -> int:
    """Expose l'etat, ET fait le travail — toutes les `reconcile_every`.

    Les deux dans le meme processus, volontairement : deux commandes a lancer
    pour un seul demon, c'est une de trop a oublier. Et c'est exactement ce qui
    s'est produit sur le runner d'origine — `serve` seul a tourne une nuit
    entiere sans rien faire.
    """
    import uvicorn  # noqa: PLC0415

    from reviewer.output.api import create_app  # noqa: PLC0415

    store = StateStore(runner.state_db)
    journal = Journal(runner.logs_dir)
    _reprendre_les_baux(store, journal)

    reveil = Reveil() if travailler else None
    app = create_app(runner, profils, store, journal, reveil=reveil)
    print(f"API locale : http://{runner.api.bind}:{runner.api.port}")
    print("  console : /   etat : /jobs   flux : /events   sante : /health")
    if travailler:
        m = int(runner.wake.reconcile_every // 60)
        quand = f"{m} min" if m else f"{int(runner.wake.reconcile_every)} s"
        print(f"  Travail : balayage toutes les {quand}, "
              f"{limit} job(s) par passage, {runner.max_parallel} de front.")
        if not runner.writes_enabled:
            print("  writes_enabled: false — les prompts sont construits, "
                  "aucun agent n'est lance.")
    else:
        # Le DIRE, sinon un demon muet passe pour un demon au repos.
        print("  --no-work : cette commande n'execute AUCUN job.")
    for nom, profile in profils.items():
        print(f"  [{nom}] {_dire_le_moteur(profile).strip()}")

    async def _ensemble() -> None:
        # `Server.serve()` plutot que `uvicorn.run()` : le second cree sa propre
        # boucle d'evenements et ne laisse rien tourner a cote. On veut les deux
        # dans la meme boucle, et surtout on veut que l'arret de l'un arrete
        # l'autre — un travailleur orphelin continuerait a ecrire sur la forge
        # apres que l'API a rendu la main.
        serveur = uvicorn.Server(uvicorn.Config(
            app, host=runner.api.bind, port=runner.api.port, log_level="warning"))
        taches = [asyncio.create_task(serveur.serve())]
        if travailler:
            taches.append(asyncio.create_task(
                _boucle_de_travail(runner, profils, limit, reveil)))
        try:
            await asyncio.gather(*taches)
        finally:
            for t in taches:
                t.cancel()
            # `gather` propage la premiere exception et LAISSE les autres taches
            # rendre la leur dans le vide — Python les signale alors par un
            # « Task exception was never retrieved » suivi d'une trace complete,
            # au milieu de laquelle le message utile se perd. On les recolte.
            await asyncio.gather(*taches, return_exceptions=True)

    try:
        asyncio.run(_ensemble())
    except KeyboardInterrupt:
        pass
    except SystemExit:
        # uvicorn quitte par `sys.exit(3)` quand le port est pris. Le cas est
        # BANAL — un demon deja lance — et merite mieux qu'une trace de trente
        # lignes ou l'on cherche la cause.
        print(f"\n!! le port {runner.api.port} est deja pris : un demon tourne "
              "probablement deja.\n"
              f"   Verifier : curl http://127.0.0.1:{runner.api.port}/health\n"
              "   Ou changer `api.port` dans runner.yaml.", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


# ── surcharges et diagnostic ────────────────────────────────────────────────

def _surcharger(profils: dict[str, ProfileConfig], *, model: str | None,
                effort: str | None) -> dict[str, ProfileConfig]:
    """Applique une surcharge de ligne de commande a tous les profils.

    On rend un profil MODIFIE plutot que de faire circuler deux valeurs de plus
    jusqu'au SDK : tout ce qui lit `profile.model` en aval — le journal, le
    diagnostic, l'appel au SDK — voit alors la valeur EFFECTIVE. Faire voyager
    la surcharge a part creerait deux verites, et le journal finirait par
    afficher le reglage du fichier pendant qu'un autre tourne.
    """
    if model is None and effort is None:
        return profils
    changements = {k: v for k, v in (("model", model), ("effort", effort))
                   if v is not None}

    # On VALIDE sur une sonde minimale, puis on applique par copie.
    #
    # Revalider le profil entier (`model_validate(model_dump())`) est faux :
    # `model_dump` rend les durees deja analysees — `nudge_after: 600.0` — que
    # la validation REFUSE justement sous forme de nombre nu, parce qu'un nombre
    # sans unite ne dit pas s'il s'agit de secondes ou de minutes.
    #
    # La sonde fait tourner les VRAIS validateurs des deux champs surcharges,
    # sans dupliquer leurs regles ici — un second jeu de regles finirait par
    # diverger du premier.
    try:
        ProfileConfig.model_validate({
            "project": "surcharge", "workspace": ".",
            "forge": {"org": "sonde"}, **changements,
        })
    except ValidationError as e:
        raise ConfigError(
            "surcharge de ligne de commande invalide :\n"
            + "\n".join(f"  {'.'.join(str(p) for p in err['loc'])} : {err['msg']}"
                        for err in e.errors())
        ) from e

    # Une surcharge de ligne de commande doit gagner sur TOUT, y compris sur la
    # table par severite — sinon `--model X` ne gouvernerait plus les P1 des
    # qu'une entree les nomme, et on croirait tourner sur X alors que non.
    #
    # On neutralise donc le champ surcharge dans chaque entree, et LUI SEUL :
    # `--effort max` ne doit pas effacer le modele choisi pour les P1.
    def _sans_le_champ_surcharge(
            table: dict[str, MoteurConfig]) -> dict[str, MoteurConfig]:
        vide = {k: None for k in changements}
        return {sev: m.model_copy(update=vide) for sev, m in table.items()}

    # `model_copy` ne revalide pas : c'est ce qu'on veut ici, le profil d'origine
    # a deja ete valide au chargement et n'a pas change ailleurs.
    return {nom: p.model_copy(update={
        **changements,
        "per_severity": _sans_le_champ_surcharge(p.per_severity),
    }) for nom, p in profils.items()}


def _dire_le_moteur(profile: ProfileConfig) -> str:
    """Une ligne qui dit avec QUOI le travail va etre fait.

    Le premier job reel du runner d'origine a tourne sur `claude-sonnet-5` sans
    que rien nulle part ne l'annonce : il a fallu relire un transcrit de session
    pour le savoir. Un reglage qui gouverne le cout et la qualite se lit dans la
    sortie.
    """
    modele = profile.model or "defaut du CLI"
    effort = profile.effort or "defaut du CLI"
    lignes = [f"    moteur : modele={modele}, effort={effort}, "
              f"max_turns={profile.max_turns}, "
              f"max_minutes/job={profile.budget.max_minutes_per_job}"]
    # La table par severite change le moteur SANS que le reglage global bouge :
    # ne montrer que le global ferait croire qu'un P1 tourne comme un P3.
    for sev in sorted(profile.per_severity, key=lambda k: Severity[k].value):
        m, e = profile.moteur(Severity[sev])
        lignes.append(f"             {sev:<8} modele={m or 'defaut du CLI'}, "
                      f"effort={e or 'defaut du CLI'}")
    return "\n".join(lignes)


def _diagnostic(profile: ProfileConfig, runner: RunnerConfig) -> list[str]:
    """Ce qui est VALIDE mais inoperant, profil par profil.

    Distinction qui a coute une demi-journee sur le runner d'origine : `check`
    disait « OK » sur une configuration ou l'agent ne pouvait rien faire — pas
    de jeton d'ecriture, donc pas de push, pas de reponse, pas de notification.
    Une configuration correcte et une configuration EFFICACE ne sont pas la meme
    propriete, et ne pas les distinguer fait chercher la panne dans le code.
    """
    manques: list[str] = []
    p = profile.project

    if not runner.writes_enabled:
        manques.append(
            "[machine] writes_enabled: false — le demon construit le prompt et "
            "s'arrete. Aucun agent n'est lance."
        )
    # Le jeton du SDK est verifie ICI, et pas seulement au lancement de l'agent.
    # Sans lui, `run_agent` leve au moment de resoudre la variable, l'exception
    # est rattrapee comme n'importe quelle panne de job, et l'echec se lit
    # « l'agent n'a pas conclu » — un message qui envoie chercher la cause dans
    # le prompt ou le modele, jamais dans une variable d'environnement absente.
    if runner.claude.oauth_token is not None:
        try:
            runner.claude.oauth_token.resolve()
        except ConfigError as e:
            manques.append(f"[machine] {e} — aucun agent ne pourra demarrer.")
    elif runner.claude.api_key is None:
        manques.append(
            "[machine] ni claude.oauth_token ni claude.api_key : le SDK n'aura "
            "aucun identifiant."
        )
    if profile.forge.token_write is None:
        manques.append(
            f"[{p}] forge.token_write absent — l'agent corrigera dans son "
            "worktree, sans pousser, sans ouvrir de PR et sans repondre aux "
            "fils. C'est le reglage qui rend le travail invisible."
        )
    else:
        try:
            profile.forge.token_write.resolve()
        except ConfigError as e:
            manques.append(f"[{p}] {e}")
    if profile.human.notify is None:
        manques.append(
            f"[{p}] human.notify absent — quand l'agent demandera un arbitrage, "
            "il ecrira dans le fil sans mentionner personne, donc sans que "
            "personne soit prevenu."
        )
    # Un worktree place sous un repertoire CACHE casse la decouverte de tests de
    # tout outil qui construit un glob depuis sa racine. Mesure du 27/08/2026 :
    # `npm run test:ci` rendait « No tests found » dans le worktree pendant que
    # les 131 memes tests passaient dans le depot principal.
    if caches := segments_caches(runner.worktrees_root):
        manques.append(
            f"[machine] worktrees_root passe par « {', '.join(caches)} », "
            "repertoire(s) cache(s). jest y echappe le point en expansant "
            "`<rootDir>` et ne trouve plus AUCUN test : les verifications d'un "
            "depot JavaScript echoueront quoi que fasse l'agent. Deplacer la "
            "racine hors d'un repertoire commencant par un point."
        )

    # Confiance des espaces de travail. `check` ne l'ACCORDE pas — une commande
    # de diagnostic qui modifie l'etat qu'elle diagnostique n'est plus un
    # diagnostic — mais elle doit le dire : sans confiance, le
    # `.claude/settings.json` du depot est ignore, hooks compris.
    if runner.claude.config_dir is not None:
        ecrivables = [d.path for d in profile.repos_by_access(Access.WRITE).values()
                      if d.path.exists()]
        if manquants := untrusted(runner.claude.config_dir, ecrivables):
            if runner.claude.trust_workspaces:
                manques.append(
                    f"[{p}] {len(manquants)} depot(s) pas encore fiables dans "
                    f"{runner.claude.config_dir} — le prochain « run » les "
                    "marquera ; d'ici la, leurs hooks ne tournent pas."
                )
            else:
                manques.append(
                    f"[{p}] claude.trust_workspaces: false et {len(manquants)} "
                    "depot(s) non fiables : leur `.claude/settings.json` est "
                    "ignore, HOOKS COMPRIS. L'agent ecrira sans les garde-fous "
                    "du depot."
                )

    for nom, depot in profile.repos_by_access(Access.WRITE).items():
        if not depot.checks:
            manques.append(
                f"[{p}] depot « {nom} » en ecriture sans aucun check : le "
                "correctif sera commite sans avoir ete valide localement."
            )
        if not depot.path.exists():
            manques.append(
                f"[{p}] depot « {nom} » : {depot.path} est introuvable — aucun "
                "worktree ne pourra en etre derive."
            )
    return manques


# ── entree ──────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="reviewer", description=__doc__)
    p.add_argument("-c", "--config", type=Path, default=Path("runner.yaml"),
                   help="chemin de runner.yaml (defaut : ./runner.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="balaye et affiche l'etat de chaque PR")
    s.add_argument("--profile", help="ne traiter qu'un profil")

    sub.add_parser("check", help="valide la configuration et sort")

    sub.add_parser("init", help="assistant d'installation : pose les questions "
                                "et ecrit runner.yaml + le profil")

    sv = sub.add_parser(
        "serve", help="fait tourner le demon : travail periodique + API locale")
    sv.add_argument("--limit", type=int, default=3,
                    help="jobs maximum par passage de balayage (defaut : 3)")
    sv.add_argument("--no-work", action="store_true",
                    help="n'exposer que l'API, sans executer de job")

    r = sub.add_parser("run", help="balaye PUIS traite le travail identifie")
    r.add_argument("--profile", help="ne traiter qu'un profil")
    r.add_argument("--limit", type=int, default=3,
                   help="nombre maximal de jobs par passage (defaut : 3 ; le "
                        "plafond du jour du profil reste la borne dure)")
    for sp in (r, sub.choices["serve"]):
        sp.add_argument("--model", help="modele du SDK pour ce lancement "
                                        "(ex. claude-opus-5). Surcharge le profil.")
        sp.add_argument("--effort", choices=EFFORTS,
                        help="niveau de raisonnement. Surcharge le profil.")

    args = p.parse_args(argv)

    if args.cmd == "init":
        # AVANT `_charger` : c'est `init` qui cree le fichier qu'on chargerait,
        # et exiger une configuration valide pour lancer l'assistant qui la
        # produit serait un cercle.
        from reviewer.bootstrap import Abandon, assistant  # noqa: PLC0415

        try:
            return assistant(args.config)
        except Abandon:
            print(file=sys.stderr)
            print("Interrompu. Rien n'a ete ecrit.", file=sys.stderr)
            return 130

    # `serve` sans configuration ne s'arrete pas : il sert de quoi en faire une.
    # Les autres commandes, si — `status` ou `run` sur rien n'auraient aucun sens,
    # et leur message dit deja quoi lancer.
    if args.cmd == "serve" and not args.config.exists():
        return _servir_installation(args.config)

    try:
        runner, profils = _charger(args.config)
    except ConfigError as e:
        print(f"Configuration invalide :\n{e}", file=sys.stderr)
        return 2

    if args.cmd == "check":
        print(f"runner.yaml : OK  (writes_enabled={runner.writes_enabled})")
        print(f"  points de reprise : {_chemin_checkpoints(runner)}")
        manques: list[str] = []
        for nom, prof in profils.items():
            e = len(prof.repos_by_access(Access.WRITE))
            c = len(prof.repos_by_access(Access.CONTEXT))
            i = len(prof.repos_by_access(Access.IGNORE))
            print(f"  profil {nom:<16} {e} ecriture / {c} contexte / {i} ignore")
            # Une configuration VALIDE peut etre inoperante : c'est le cas exact
            # qui a fait croire que l'agent etait casse. On liste donc ce qui
            # manque pour qu'il AGISSE, en plus de ce qui manque pour qu'il
            # demarre.
            manques += _diagnostic(prof, runner)
        if manques:
            print("\nCe qui empeche encore le demon d'agir :")
            for m in manques:
                print(f"  - {m}")
        else:
            print("\nRien ne manque : le demon peut corriger, pousser et repondre.")
        return 0 if profils else 1

    if args.cmd == "serve":
        try:
            profils = _surcharger(profils, model=args.model, effort=args.effort)
        except ConfigError as e:
            print(str(e), file=sys.stderr)
            return 2
        return _serve(runner, profils, args.limit, travailler=not args.no_work)

    if args.cmd == "run":
        try:
            profils = _surcharger(profils, model=args.model, effort=args.effort)
            return asyncio.run(_run(runner, profils, args.profile, args.limit))
        except ConfigError as e:
            print(str(e), file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            return 130

    try:
        return asyncio.run(_status(runner, profils, args.profile))
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
