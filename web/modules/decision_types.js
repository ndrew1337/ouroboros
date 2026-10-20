/** Dependency-free owner-decision contracts: request/reply shapes and their quiz frames.
 * Re-exported by ./api_types.js; the decision family stays here under one owner. */

/**
 * POST /api/decisions body — the ONE answer ingress for owner decision cards
 * (decision families quiz:/routing:/interaction:/model_wait:/card:). request_id is the
 * idempotency key; a replay returns the recorded confirmation. option_index is
 * optional for a quiz free answer and for typed model_wait actions. A quiz
 * free answer sends a non-empty comment; model_wait sends revision and action. A quiz
 * action=dismiss removes an accepted late answer of unknown delivery from current
 * questions, carrying no answer or reason (never delivered, never resent). A living card's press
 * (card:{card_id}:{action_id}) carries the card `revision` it was drawn from (a newer card answers 409
 * card_stale) and `values` only for a form action.
 * @typedef {Object} DecisionRequest
 * @property {string} request_id
 * @property {string} decision_id
 * @property {number=} option_index
 * @property {string=} comment
 * @property {number=} revision
 * @property {string=} action
 * @property {string=} reason Explicit quiz closure explanation.
 * @property {boolean=} auto_continue
 * @property {string=} model
 * @property {string=} credential_profile_id
 * @property {boolean=} use_local
 * @property {boolean=} persist_role
 * @property {Object=} values  // card: form field values (JSON scalars)
 */

/**
 * Answer-ingress reply; 409 carries the card's truthful lifecycle state so a
 * late click settles the card instead of inviting retries.
 * @typedef {Object} DecisionResponse
 * @property {string=} closed_actor
 * @property {string=} closed_at
 * @property {string=} closed_reason
 * @property {string=} closed_request_id
 * @property {Object=} question_delivery
 * @property {string=} request_id
 * @property {boolean=} applied
 * @property {(boolean|null)=} saved
 * @property {Object=} wait
 * @property {string=} reason_code
 * @property {boolean=} ok
 * @property {string=} decision_id
 * @property {string=} state
 * @property {number=} answered_index
 * @property {string=} comment
 * @property {boolean=} duplicate
 * @property {boolean=} answered_after_terminal
 * @property {boolean=} forwarded
 * @property {string=} error
 * @property {string=} dispatched
 * @property {string=} task_id
 * @property {string=} latest_status
 * @property {string=} reason
 * @property {string=} detail
 * @property {string=} cause  // the owner-facing sentence for a refused routing act (409 dispatch_rejected)
 * @property {string=} reasoning_effort  // a New task picked from the card: the start its admitted row requests; never on a steer
 * @property {('delivered'|'forwarded'|'owed')=} delivery  // card: where the press went (owed answers 202)
 * @property {string=} card_id
 * @property {number=} revision
 * @property {Object=} winner  // card 409 card_action_once: {request_id, at, delivery} of the press that counted
 */

/**
 * One asked field of an integration-secret request: its public id and the Settings key it writes.
 * @typedef {Object} SecretRequestField
 * @property {string} id
 * @property {string} setting_key
 */

/**
 * An integration-secret request (showSecretHitl) on its question card; metadata only, never a value.
 * @typedef {Object} SecretRequestMeta
 * @property {string} plugin_name
 * @property {SecretRequestField[]} fields
 * @property {string} content_hash
 */

/**
 * The host's typed completion of a secret request (secret_filled after Settings kept the values,
 * secret_cancelled with nothing saved). grant_missing names saved keys the skill's current grant
 * does not cover; saving grants nothing.
 * @typedef {Object} SecretOutcome
 * @property {"secret_filled"|"secret_cancelled"} event
 * @property {string} pluginName
 * @property {string[]} fields
 * @property {string} at
 * @property {string[]} settings_keys
 * @property {string[]=} grant_missing
 * @property {boolean=} grant_unknown
 * @property {boolean=} grant_unsupported
 * @property {boolean=} interrupted_save
 */

/**
 * The screenshot's event payload: plugin and field ids, never a value.
 * @typedef {Object} SecretEvent
 * @property {string} pluginName
 * @property {string[]} fields
 */

/**
 * POST /api/decisions/secret body — saveSecretToken. values carries exactly the asked field ids
 * on save; action "cancel" carries none. Reuse request_id only for unchanged values.
 * @typedef {Object} SecretDecisionRequest
 * @property {string} request_id
 * @property {string} decision_id
 * @property {("save"|"cancel")=} action
 * @property {Object<string, string>=} values
 */

/**
 * Secret form reply; saved is present on both sides of the Settings commit. saved true with
 * secret_save_unverified or secret_completion_unrecorded is a written value not reported filled;
 * saved null with secret_save_unconfirmed is an attempt of unknown outcome. Read status first;
 * only a new request saves again.
 * @typedef {Object} SecretDecisionResponse
 * @property {boolean=} ok
 * @property {(boolean|null)=} saved
 * @property {string=} decision_id
 * @property {string=} state
 * @property {boolean=} duplicate
 * @property {SecretEvent=} secret_filled
 * @property {SecretEvent=} secret_cancelled
 * @property {SecretOutcome=} secret_outcome
 * @property {string[]=} settings_keys
 * @property {string[]=} grant_missing
 * @property {boolean=} grant_unknown
 * @property {boolean=} grant_unsupported
 * @property {boolean=} interrupted_save
 * @property {boolean=} save_unconfirmed
 * @property {string=} closed_reason
 * @property {string=} closed_at
 * @property {string=} closed_actor
 * @property {string=} closed_request_id
 * @property {string=} closed_by_task_id
 * @property {Object=} question_delivery
 * @property {string=} error
 * @property {string=} reason_code
 */

/**
 * @typedef {Object} QuizOption
 * @property {string} label
 * @property {string=} detail
 * @property {boolean=} recommended
 */

/**
 * @typedef {Object} QuizOutbound
 * @property {Array<Object>=} materials Validated existing artifact references.
 * @property {{kind:'publication'|'answer'|'closure',state:'owed'|'delivered',reason_code?:string,dismissed?:boolean}=} question_delivery Canonical settlement delivery fact.
 * @property {string=} closed_reason
 * @property {string=} closed_at
 * @property {string=} closed_by_task_id
 * @property {string=} closed_actor
 * @property {string=} closed_request_id
 * @property {"quiz"} type
 * @property {"assistant"} role
 * @property {string} quiz_id
 * @property {string} question
 * @property {QuizOption[]} options
 * @property {string} stake
 * @property {string} assumption
 * @property {boolean=} wait_for_answer
 * @property {string} state
 * @property {string} ts
 * @property {number=} answered_index
 * @property {string=} comment
 * @property {string=} host_facts
 * @property {number=} chat_id
 * @property {string=} task_id
 * @property {boolean=} project_thread
 * @property {Object=} transport
 * @property {string=} row_address
 * @property {Object=} reply_to
 * @property {Object=} relay
 * @property {string=} reply_to_relay
 * @property {string=} kind
 * @property {import('./decision_types.js').SecretRequestMeta=} secret_request An integration-secret request: answered only by its protected form.
 */

/**
 * Lifecycle update for an already-rendered quiz card (WS "quiz_state") —
 * a separate discriminator so a state change never dedupes as (or spawns)
 * a second card. answered_index rides only with state "answered".
 * @typedef {Object} QuizStateOutbound
 * @property {{kind:'publication'|'answer'|'closure',state:'owed'|'delivered',reason_code?:string,dismissed?:boolean}=} question_delivery Canonical settlement delivery fact.
 * @property {string=} closed_reason
 * @property {string=} closed_at
 * @property {string=} closed_by_task_id
 * @property {string=} closed_actor
 * @property {string=} closed_request_id
 * @property {"quiz_state"} type
 * @property {string} quiz_id
 * @property {string} task_id
 * @property {string} state
 * @property {string} ts
 * @property {number=} answered_index
 * @property {string=} comment
 *   The owner's recorded free-text answer, when one was recorded.
 * @property {boolean=} wait_for_answer
 *   False once a bounded wait closed and the task resumed; the card stays open and answerable.
 * @property {number=} chat_id
 * @property {import('./decision_types.js').SecretOutcome=} secret_outcome A secret request's typed completion, metadata only.
 */

