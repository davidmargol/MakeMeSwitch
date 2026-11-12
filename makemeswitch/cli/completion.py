"""prompt_toolkit completion for the MakeMeSwitch CLI."""
from __future__ import annotations

from prompt_toolkit.completion import Completer, Completion

from makemeswitch.cli import pipe as pipe_filters
from makemeswitch.cli.parser import CommandParser


class SwitchCompleter(Completer):
    def __init__(self, parser: CommandParser, session):
        self.parser = parser
        self.session = session

    def get_completions(self, document, complete_event):
        line = document.text_before_cursor
        if "|" in line:
            yield from self._pipe_completions(line)
            return
        tokens = self.parser.tokenize(line)
        mode = self.session.mode
        context = tokens[:-1] if tokens and not line.endswith(" ") else tokens
        candidates = self.parser.candidates(context, mode)
        prefix = "" if line.endswith(" ") or not tokens else tokens[-1]
        # This command accepts trailing arguments, which have no parser nodes.
        # Resolve the actual spec (including mode and abbreviations), rather
        # than offering trunk arguments for unrelated commands/modes.
        match = self.parser.resolve(context, mode)
        if len(context) >= 4 and match.spec and match.spec.path == ("switchport", "trunk", "allowed", "vlan"):
            if not match.argv:
                candidates = ["<vlan>", "add", "all", "none", "remove"]
            elif len(match.argv) == 1 and match.argv[0].lower() in ("add", "remove"):
                candidates = ["<vlan>"]
        for candidate in candidates:
            if candidate.startswith("<"):
                dynamic = self._dynamic(candidate)
                for value in dynamic:
                    if value.lower().startswith(prefix.lower()):
                        yield Completion(value, start_position=-len(prefix))
            elif candidate.lower().startswith(prefix.lower()):
                yield Completion(candidate, start_position=-len(prefix))

    def _pipe_completions(self, line: str):
        """Complete ``| include``/``| begin``/… after a pipe."""
        rows = pipe_filters.filter_candidates(line)
        tail = line.rsplit("|", 1)[1]
        word = tail.strip() if " " not in tail.strip() else ""
        for candidate, description in rows:
            yield Completion(candidate, start_position=-len(word), display_meta=description)

    def _dynamic(self, placeholder: str) -> list[str]:
        engine = self.session.engine
        if placeholder == "<switchport-mode>":
            return ["access", "trunk"]
        if placeholder in ("<interface>",):
            names = sorted(engine.config.interfaces.keys())
            return names + ["port-channel1", "vlan1"]
        if placeholder in ("<vlan>",):
            return [str(v) for v in sorted(engine.config.vlans)]
        if placeholder in ("<port>",):
            return sorted(engine.ports.keys())
        if placeholder in ("<channel>",):
            return [str(c) for c in sorted(engine.config.port_channels)] or ["1"]
        if placeholder == "<mac>":
            return []
        return []
