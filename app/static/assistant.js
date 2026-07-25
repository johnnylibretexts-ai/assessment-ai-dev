// Demo assistant panel: open/close, restore, and read the SSE answer stream.
//
// Uses fetch() + a ReadableStream reader rather than EventSource because the
// request has to be a POST (EventSource is GET-only).
//
// Every answer is written with textContent, never innerHTML. The model emits
// arbitrary text and the page has no sanitizer, so this is the difference
// between "renders plainly" and "renders whatever it was told to".
(() => {
  const launcher = document.querySelector("#assistant-launcher");
  const panel = document.querySelector("#assistant-panel");
  const log = document.querySelector("#assistant-log");
  const form = document.querySelector("#assistant-form");
  const input = document.querySelector("#assistant-input");
  const send = document.querySelector("#assistant-send");
  const closeButton = document.querySelector("#assistant-close");
  const resetButton = document.querySelector("#assistant-reset");

  if (!launcher || !panel || !log || !form || !input || !send) return;

  let restored = false;
  let busy = false;

  const clearEmptyState = () => {
    const empty = log.querySelector(".assistant-empty");
    if (empty) empty.remove();
  };

  const scrollToEnd = () => {
    log.scrollTop = log.scrollHeight;
  };

  const addTurn = (role, text) => {
    clearEmptyState();
    const turn = document.createElement("div");
    turn.className = `assistant-turn assistant-turn-${role}`;

    const who = document.createElement("p");
    who.className = "assistant-who";
    who.textContent =
      role === "user" ? "You" : role === "error" ? "Problem" : "Assistant";

    const body = document.createElement("p");
    body.className = "assistant-text";
    body.textContent = text;

    turn.append(who, body);
    log.append(turn);
    scrollToEnd();
    return body;
  };

  const setBusy = (value) => {
    busy = value;
    send.disabled = value;
    input.readOnly = value;
    panel.classList.toggle("is-busy", value);
  };

  const restore = async () => {
    if (restored) return;
    restored = true;
    try {
      const response = await fetch("/assistant/conversation", {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) return;
      const payload = await response.json();
      (payload.messages || []).forEach((message) => {
        addTurn(message.role, message.content);
      });
    } catch (error) {
      // A failed restore is not worth interrupting anyone over; the panel
      // still works for new questions.
    }
  };

  const open = () => {
    panel.hidden = false;
    launcher.setAttribute("aria-expanded", "true");
    document.body.classList.add("assistant-open");
    restore();
    input.focus();
  };

  const close = () => {
    panel.hidden = true;
    launcher.setAttribute("aria-expanded", "false");
    document.body.classList.remove("assistant-open");
    launcher.focus();
  };

  launcher.addEventListener("click", () => {
    if (panel.hidden) open();
    else close();
  });

  if (closeButton) closeButton.addEventListener("click", close);

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !panel.hidden) close();
  });

  if (resetButton) {
    resetButton.addEventListener("click", async () => {
      if (busy) return;
      try {
        await fetch("/assistant/reset", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
        });
      } catch (error) {
        return;
      }
      log.textContent = "";
      const empty = document.createElement("p");
      empty.className = "assistant-empty";
      empty.textContent = "New conversation. Ask anything.";
      log.append(empty);
      input.focus();
    });
  }

  // One SSE frame is "event: <name>\ndata: <json>\n\n". Frames can be split
  // across network chunks, so hold a buffer and only consume complete ones.
  const consumeFrames = (buffer, onFrame) => {
    let rest = buffer;
    let boundary = rest.indexOf("\n\n");
    while (boundary !== -1) {
      const frame = rest.slice(0, boundary);
      rest = rest.slice(boundary + 2);
      let name = "message";
      let data = "";
      frame.split("\n").forEach((line) => {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      });
      if (data) {
        try {
          onFrame(name, JSON.parse(data));
        } catch (error) {
          // Ignore an unparseable frame rather than killing the stream.
        }
      }
      boundary = rest.indexOf("\n\n");
    }
    return rest;
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;

    const question = input.value.trim();
    if (!question) return;

    addTurn("user", question);
    input.value = "";
    setBusy(true);

    const answer = addTurn("assistant", "");
    answer.classList.add("is-streaming");
    let received = false;

    try {
      const response = await fetch("/assistant/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, route: window.location.pathname }),
      });

      if (!response.ok || !response.body) {
        let detail = "The assistant could not answer that. Try again.";
        try {
          const problem = await response.json();
          if (problem && problem.detail) detail = problem.detail;
        } catch (error) {
          // Keep the generic message.
        }
        answer.parentElement.remove();
        addTurn("error", detail);
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        buffer = consumeFrames(buffer, (name, payload) => {
          if (name === "delta") {
            received = true;
            answer.textContent += payload;
            scrollToEnd();
          } else if (name === "error") {
            answer.parentElement.remove();
            addTurn("error", payload);
          }
        });
      }

      if (!received && answer.parentElement) {
        answer.parentElement.remove();
      }
    } catch (error) {
      if (answer.parentElement) answer.parentElement.remove();
      addTurn("error", "The connection dropped before the answer finished.");
    } finally {
      answer.classList.remove("is-streaming");
      setBusy(false);
      input.focus();
    }
  });

  // Enter sends, Shift+Enter makes a newline -- the shape people expect from a
  // chat box, without losing the ability to write a multi-line question.
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
})();
