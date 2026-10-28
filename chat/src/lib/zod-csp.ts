// GĐ3: zod 4 (used by @assistant-ui/generative-ui) probes `new Function` for its JIT; under our CSP (no 'unsafe-eval') the
// probe is reported as a securitypolicyviolation. jitless = same validation, no eval, CSP unchanged. Must run first.
import { z } from "zod";
z.config({ jitless: true });
