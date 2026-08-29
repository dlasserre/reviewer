"""L'appel au Claude Agent SDK. Un seul point d'entree, un seul noeud du graphe.

Sorti de l'orchestration volontairement : le SDK est ce qui ECRIT LE CODE, et
c'est la seule brique que LangGraph n'orchestre pas — il l'appelle. Melanger les
deux rendrait illisible la question « qui decide » et « qui code ».

Ce module ne connait ni la forge, ni les baux, ni le graphe. Il recoit un
prompt, un worktree, un garde-fou, et rend ce que l'agent a produit.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent_runner_lg.agent.guard import Guard
from agent_runner_lg.config import ProfileConfig, RunnerConfig
from agent_runner_lg.repo.checks import outils_locaux

__all__ = ["AgentOutcome", "agent_env", "run_agent"]


@dataclass(frozen=True, slots=True)
class AgentOutcome:
    """Ce que l'appel au SDK a produit."""

    session_id: str | None = None
    subtype: str = "unknown"
    cost_usd: float | None = None
    text: str = ""
    error: str | None = None
    # Sortie contrainte par le schema de `verdict.SCHEMA`. `None` quand l'agent
    # n'a rien rendu — cas traite comme un arbitrage, jamais comme un accord.
    structured: object | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.subtype == "success"



def agent_env(runner: RunnerConfig, worktree: Path) -> dict[str, str]:
    """Environnement passe au SDK. Le SDK le FUSIONNE avec l'environnement herite.

    ── L'OUTILLAGE DU DEPOT, MESURE LE 27/08/2026 ─────────────────────────

    Les verifications du runner preparaient deja leur PATH (`outils_locaux` :
    le `Scripts`/`bin` du venv en tete). L'AGENT, lui, ne recevait rien : il
    heritait du PATH du demon, ou le venv du depot n'est pas.

    Le premier job reel l'a montre sans ambiguite. L'agent a lu le code, corrige
    trois fichiers — six `Edit` — puis a passe les tours 55 a 66 a chercher un
    interpreteur Python : `where.exe python`, `env | grep -i python`,
    `printenv VIRTUAL_ENV`, exploration de repertoires. Il a epuise `max_turns`
    sur cette recherche, et le cycle a ete perdu alors que le correctif etait
    ecrit.

    On ne peut pas demander a l'agent de « lancer les tests du depot » — ce que
    le prompt lui demande — sans lui donner de quoi les lancer. Il recoit donc
    exactement l'environnement d'outils que les verifications utilisent.

    Le PATH est reconstruit ENTIER, pas prefixe : ce dictionnaire ecrase les
    variables de meme nom cote SDK, donc un PATH partiel effacerait le reste.
    """
    env: dict[str, str] = {}
    # `ANTHROPIC_API_KEY` prime sur `CLAUDE_CODE_OAUTH_TOKEN` : la laisser
    # basculerait la facturation de l'abonnement vers l'API, en silence.
    for nom in runner.claude.scrub_env:
        env[nom] = ""
    if runner.claude.oauth_token is not None:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = runner.claude.oauth_token.resolve()
    if runner.claude.config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(runner.claude.config_dir)
    if runner.claude.disable_auto_memory:
        # La memoire automatique se charge INDEPENDAMMENT de `setting_sources` :
        # sans ce drapeau, le contexte d'un projet polluerait celui d'un autre.
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"

    if outils := outils_locaux(worktree):
        env["PATH"] = os.pathsep.join(
            [*(str(p) for p in outils), os.environ.get("PATH", "")])
        # Certains outils lisent `VIRTUAL_ENV` plutot que le PATH.
        for p in outils:
            if p.parent.name in (".venv", "venv"):
                env["VIRTUAL_ENV"] = str(p.parent)
                break
    return env


# L'argument qui NOMME ce que fait l'outil, par outil. Afficher `input` en
# entier noierait la ligne — un `Edit` porte tout le nouveau contenu du fichier
# — et ferait sortir du code source vers l'affichage sans qu'on l'ait decide.
_ARGUMENT_PARLANT = {
    "Read": "file_path", "Edit": "file_path", "Write": "file_path",
    "NotebookEdit": "notebook_path", "Glob": "pattern", "Grep": "pattern",
    "Bash": "command", "WebFetch": "url", "Task": "description",
}


def _resumer_outil(bloc) -> str:
    """« Edit app/service.py », « Bash pytest -q » — une ligne, pas un dump."""
    nom = getattr(bloc, "name", "?")
    entree = getattr(bloc, "input", None)
    if not isinstance(entree, dict):
        return nom
    valeur = entree.get(_ARGUMENT_PARLANT.get(nom, ""), "")
    if not isinstance(valeur, str) or not valeur.strip():
        return nom
    valeur = " ".join(valeur.split())
    return f"{nom} {valeur[:120]}" + ("…" if len(valeur) > 120 else "")


async def run_agent(
    prompt_text: str,
    *,
    worktree: Path,
    profile: ProfileConfig,
    runner: RunnerConfig,
    guard: Guard,
    resume: str | None = None,
    timeout_s: float = 1800.0,
    output_format: dict | None = None,
    extra_dirs: tuple[Path, ...] = (),
    model: str | None = None,
    effort: str | None = None,
    on_step: "Callable[[str, str], None] | None" = None,
) -> AgentOutcome:
    """Appelle le Claude Agent SDK dans le worktree du job.

    Import PARESSEUX : le SDK embarque un binaire, et le demon doit pouvoir
    tourner en lecture seule (lot 1) sans qu'il soit installe.
    """
    from claude_agent_sdk import (  # noqa: PLC0415
        AssistantMessage,
        ClaudeAgentOptions,
        HookMatcher,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
        query,
    )

    env = agent_env(runner, worktree)

    options = ClaudeAgentOptions(
        cwd=str(worktree),
        # `setting_sources` charge le CLAUDE.md, les agents et les hooks
        # VERSIONNES du depot. C'est ce qui fait que l'agent respecte les
        # conventions du projet sans qu'on les recopie.
        setting_sources=profile.setting_sources,
        # Les SKILLS ne suivent PAS `setting_sources` : sans ce reglage, le SDK
        # ne configure rien et on herite des defauts du CLI. « Les skills du
        # depot sont chargees » doit se lire dans le profil, pas se deduire
        # d'une absence.
        skills=profile.skills,
        plugins=[{"type": "local", "path": str(p)} for p in profile.plugins],
        # Depots `access: context` : l'agent les LIT — code, conventions,
        # `AGENTS.md` — et ne peut pas les ecrire. Sans `add_dirs`, ils sont
        # hors du `cwd` donc illisibles, et `access: context` ne voudrait plus
        # rien dire. Le garde-fou `PreToolUse` reste ce qui interdit d'y ecrire.
        add_dirs=[str(d) for d in extra_dirs],
        # `None` laisse le defaut du CLI. Le profil peut les nommer, et la ligne
        # de commande les surcharger pour un passage — c'est le meme reglage
        # qu'on veut pouvoir essayer sans editer un fichier.
        # Resolus par l'appelant a partir de la severite des remarques ; le
        # profil reste le repli. `None` laisse le defaut du CLI.
        model=model if model is not None else profile.model,
        effort=effort if effort is not None else profile.effort,
        permission_mode=profile.permission_mode,
        max_turns=profile.max_turns,
        # Defense en profondeur : ces regles sont CONTOURNABLES par prefixe
        # (`git -C . push`). La barriere reelle est le hook ci-dessous.
        disallowed_tools=[
            "Bash(git push:*)", "Bash(gh pr merge:*)", "Bash(gh pr close:*)",
            "Bash(git reset:*)", "Bash(git rebase:*)",
        ],
        hooks={"PreToolUse": [HookMatcher(hooks=[guard.as_hook()])]},
        output_format=output_format,
        resume=resume,
        env=env,
    )

    session = None
    subtype = "unknown"
    cout = None
    structure = None
    morceaux: list[str] = []

    def etape(genre: str, texte: str) -> None:
        """Signale une etape, sans jamais faire echouer le job pour ca.

        L'observation est un CONFORT : si le journal est plein, si l'abonne a
        disparu, le travail continue. L'inverse — perdre un correctif parce
        qu'on n'a pas su l'afficher — serait absurde.
        """
        if on_step is None:
            return
        try:
            on_step(genre, texte)
        except Exception:  # noqa: BLE001
            pass

    async def boucle() -> None:
        nonlocal session, subtype, cout, structure
        async for message in query(prompt=prompt_text, options=options):
            if isinstance(message, AssistantMessage):
                for bloc in message.content:
                    if isinstance(bloc, TextBlock):
                        morceaux.append(bloc.text)
                        etape("texte", bloc.text)
                    elif isinstance(bloc, ToolUseBlock):
                        etape("outil", _resumer_outil(bloc))
            elif isinstance(message, ResultMessage):
                # `session_id` est present sur CHAQUE resultat, succes comme
                # erreur : c'est ce qui permet de reprendre apres un echec.
                session = message.session_id
                subtype = message.subtype
                cout = message.total_cost_usd
                structure = message.structured_output

    try:
        await asyncio.wait_for(boucle(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return AgentOutcome(session, "timeout", cout, "".join(morceaux),
                            error=f"l'agent n'a pas conclu en {int(timeout_s)} s")
    except Exception as e:  # noqa: BLE001 — on veut l'erreur telle quelle
        return AgentOutcome(session, subtype, cout, "".join(morceaux),
                            error=f"{type(e).__name__} : {e}")
    return AgentOutcome(session, subtype, cout, "".join(morceaux),
                        structured=structure)


