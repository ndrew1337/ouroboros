import { plainCauseText } from './utils.js';

// Twin of supervisor.cancel_publication.cancel_cause_clauses. IDs stay in
// cancel_origin/lineage details; a compact sentence never guesses an actor.
export const CANCEL_SOURCE_PHRASES = {
    http_single: 'Stopped from the app (Stop now)',
    http_cascade: 'Stopped from the app (Stop now)',
    http_graceful: 'Stopped from the app (Wrap up)',
    cascade_descendant: 'Stopped with the task tree it belongs to',
};

export function cancelCauseClauses(origin, record = {}) {
    const source = String(origin.source || '');
    const asked = String(origin.requested_by || '');
    const self = String(record.task_id || record.id || record.subagent_task_id || '');
    const parent = String(record.parent_task_id || '');
    const root = String(record.root_task_id || '');
    const actor = origin.request_origin?.kind === 'agent_task' && origin.request_origin.task_id;
    const relation = asked && asked !== self && asked === parent ? 'Stopped with its parent task'
        : asked && asked !== self && parent && asked === root ? 'Stopped with an ancestor task' : '';
    return [
        Object.hasOwn(CANCEL_SOURCE_PHRASES, source) ? CANCEL_SOURCE_PHRASES[source] : source,
        plainCauseText(origin.reason),
        origin.scope === 'cascade' ? 'this task and its sub-tasks' : '',
        relation,
        actor ? 'Requested by a task' : '',
    ];
}
