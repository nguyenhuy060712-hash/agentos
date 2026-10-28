import type { VoiceConfig, VoiceGrant } from "@/voice/types";
import type { VoiceHttpError } from "@/voice/api";
export type VoiceUiState = {
  config: VoiceConfig | null; model: string | null; grantError: VoiceHttpError | null; session: { grant: VoiceGrant; startedAt: number } | null;
  last: { seconds: number; cost_usd: number; reason: string } | null; bargeIns: number[]; log: string[]; spend?: { session: number; total: number };
};
