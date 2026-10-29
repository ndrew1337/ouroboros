"""Room liveness from captured rows and addressed task views; no history scan or sealing authority."""
from ouroboros.dialogue_provenance import row_class


def considered_through(entries, lineage):
    """Last mind-authored quiet finish in stored order, also when its own row is already sealed."""
    positions = []
    for _address, row, pos in entries:
        own = row_class(row, pos=pos, **lineage)["author"]["kind"] == "ouroboros"
        canonical = (row.get("summary_kind") == "terminal_root_projection"
                     and row.get("outcome_authority") == "canonical_task_result_after_finalization"
                     and row.get("terminal_origin") == "model_final"
                     and not row.get("subagent_task_id") and row.get("delegation_role") != "subagent")
        if row.get("quiet_finish") is True and (own or canonical):
            positions.append(pos)
    return max(positions, default=-1)


def unconsidered_people(entries, lineage, quiet_pos):
    return sum(pos > quiet_pos and (cls := row_class(row, pos=pos, **lineage))["lane"] == 1
               and cls["author"]["kind"] == "human" for _address, row, pos in entries)
