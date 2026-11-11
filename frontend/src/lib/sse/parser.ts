export interface SSEMessage {
  id?: string;
  event: string;
  data: string;
}

/**
 * Incremental parser for the text/event-stream format. Feed decoded chunks to
 * `push`; it returns the complete messages found so far.
 */
export class SSEParser {
  private buffer = "";
  private id: string | undefined;
  private event = "";
  private data: string[] = [];

  push(chunk: string): SSEMessage[] {
    this.buffer += chunk;
    const lines = this.buffer.split(/\r\n|\n|\r/);
    // The last element is an incomplete line (or "" after a terminator).
    this.buffer = lines.pop() ?? "";

    const messages: SSEMessage[] = [];
    for (const line of lines) {
      if (line === "") {
        const message = this.flush();
        if (message) messages.push(message);
      } else {
        this.field(line);
      }
    }
    return messages;
  }

  private field(line: string): void {
    if (line.startsWith(":")) return; // comment / keep-alive
    const sep = line.indexOf(":");
    const name = sep === -1 ? line : line.slice(0, sep);
    let value = sep === -1 ? "" : line.slice(sep + 1);
    if (value.startsWith(" ")) value = value.slice(1);

    switch (name) {
      case "id":
        if (!value.includes("\0")) this.id = value;
        break;
      case "event":
        this.event = value;
        break;
      case "data":
        this.data.push(value);
        break;
      default:
        break; // "retry" and unknown fields are ignored; backoff is client-controlled
    }
  }

  private flush(): SSEMessage | null {
    const message =
      this.data.length > 0
        ? { id: this.id, event: this.event || "message", data: this.data.join("\n") }
        : null;
    this.event = "";
    this.data = [];
    return message;
  }
}
