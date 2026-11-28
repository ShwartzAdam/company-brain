"""streamlit run scripts/ui.py   (MCP server must be running: python -m server.server)
Thin wrapper over the same handle_turn the tests call. Caller/group are sidebar inputs, never message text."""
import sys
import uuid
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from langfuse import get_client  # noqa: E402

from brain.agent import handle_turn  # noqa: E402

AS_OF = "2026-07-01T12:00:00Z"

caller = st.sidebar.text_input("Caller id", "E010")
group = st.sidebar.selectbox("User group", ["UG_REGULAR", "UG_HR", "UG_IT"])
if st.sidebar.button("New thread"):
    st.session_state.clear()

st.session_state.setdefault("thread", f"ui-{uuid.uuid4().hex[:8]}")
st.session_state.setdefault("log", [])

for role, text in st.session_state.log:
    st.chat_message(role).write(text)

if q := st.chat_input("Ask the brain"):
    st.chat_message("user").write(q)
    with st.spinner("Thinking..."):
        r = handle_turn(caller, group, st.session_state.thread, q, AS_OF)
    get_client().flush()
    with st.chat_message("assistant"):
        st.write(r.answer)
        with st.expander(f"{r.status} · {r.intent} · {r.scope}"):
            st.write({"tools": r.tool_sequence, "sources": r.sources, "request_id": r.request_id})
    st.session_state.log += [("user", q), ("assistant", r.answer)]
