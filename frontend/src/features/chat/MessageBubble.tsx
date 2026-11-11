import { AlertCircle } from "lucide-react";
import { cn, formatTime } from "@/lib/utils";
import type { DisplayMessage } from "./types";

interface MessageBubbleProps {
  message: DisplayMessage;
  onRetry?: (clientId: string) => void;
}

export function MessageBubble({ message, onRetry }: MessageBubbleProps) {
  if (message.role === "system") {
    return <p className="text-center text-xs italic text-slate-500">{message.text}</p>;
  }

  const mine = message.role === "user";
  const failed = message.delivery === "failed";

  return (
    <div className={cn("flex flex-col gap-1", mine ? "items-end" : "items-start")}>
      {!mine ? (
        <span className="text-xs font-medium capitalize text-slate-500">
          {message.agent ?? "agent"}
        </span>
      ) : null}
      <div
        className={cn(
          "max-w-[85%] whitespace-pre-wrap break-words rounded-2xl px-3.5 py-2 text-sm",
          mine ? "bg-paypal-blue text-white" : "bg-slate-100 text-slate-900",
          message.is_question && "ring-2 ring-amber-300",
          message.delivery === "sending" && "opacity-70",
          failed && "bg-red-50 text-red-900 ring-1 ring-red-200",
        )}
      >
        {message.text}
      </div>
      <div className="flex items-center gap-2 text-xs text-slate-500">
        {failed ? (
          <>
            <AlertCircle className="h-3.5 w-3.5 text-red-600" aria-hidden />
            <span className="text-red-700">Failed to send</span>
            {message.client_id && onRetry ? (
              <button
                type="button"
                onClick={() => onRetry(message.client_id as string)}
                className="font-medium text-paypal-lightblue hover:underline"
              >
                Retry
              </button>
            ) : null}
          </>
        ) : message.delivery === "sending" ? (
          <span>Sending…</span>
        ) : (
          <span>{formatTime(message.created_at)}</span>
        )}
      </div>
    </div>
  );
}
