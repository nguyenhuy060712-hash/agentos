// assistant-ui registry (base style), copied verbatim 20/10/2026 from https://r.assistant-ui.com/base/files/ (MIT). Not a fork: shadcn-style copy-in.
"use client";

import { memo } from "react";
import type { TextMessagePartComponent } from "@assistant-ui/react";
import type { DirectiveFormatter } from "@assistant-ui/react";
import { defaultDirectiveFormatter } from "@assistant-ui/react";
import {
  createDirectiveText as createDirectiveTextBase,
  type CreateDirectiveTextOptions,
} from "./directive-text";

export type {
  CreateDirectiveTextOptions,
  DirectiveTextFormatter,
  DirectiveTextSegment,
} from "./directive-text";

/** Creates a `Text` message part component that parses directive syntax and renders inline chips. */
export function createDirectiveText(
  formatter: DirectiveFormatter,
  options?: CreateDirectiveTextOptions,
): TextMessagePartComponent {
  return createDirectiveTextBase(formatter, options);
}

/** `Text` message part component that renders directive syntax as inline chips. */
export const DirectiveText: TextMessagePartComponent = memo(
  createDirectiveTextBase(defaultDirectiveFormatter),
);
