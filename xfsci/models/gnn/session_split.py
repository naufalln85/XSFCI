"""Deterministic session-level split shared by feature scaling and GNN training."""


def split_session_ids(session_ids, train_ratio: float = 0.70, val_ratio: float = 0.15):
    """Return ordered train/validation/test session IDs without splitting a session.

    Session IDs must be supplied in chronological order. With fewer than three
    sessions, retain the established fallback behavior; the trainer's class
    coverage gate prevents a weak split from being accepted as a model.
    """
    sessions = list(dict.fromkeys(str(value) for value in session_ids if value is not None))
    if len(sessions) >= 3:
        n_train = max(1, int(len(sessions) * train_ratio))
        n_val = max(1, int(len(sessions) * val_ratio))
        if n_train + n_val >= len(sessions):
            n_train = max(1, len(sessions) - 2)
            n_val = 1
        return (
            sessions[:n_train],
            sessions[n_train:n_train + n_val],
            sessions[n_train + n_val:],
        )
    if len(sessions) == 2:
        return [sessions[0]], [], [sessions[1]]
    return sessions, [], []
