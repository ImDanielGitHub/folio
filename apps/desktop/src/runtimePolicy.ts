export type RuntimeAvailabilityMode =
  | "checking"
  | "live"
  | "fixture"
  | "offline"
  | "degraded";

export type OnboardingPolicyInput = {
  mode: RuntimeAvailabilityMode;
  explicitDemo: boolean;
  forcedOnboarding: boolean;
  rememberedOnboarding: boolean;
  hasAuthoritativeSnapshot: boolean;
};

export function onboardingVisibleAfterProbe(input: OnboardingPolicyInput): boolean {
  if (input.explicitDemo) return false;
  if (input.forcedOnboarding) return true;
  if (!input.rememberedOnboarding) return true;
  if (input.mode === "live") return false;
  return !input.hasAuthoritativeSnapshot;
}

export function initialOnboardingVisible(
  explicitDemo: boolean,
  forcedOnboarding: boolean,
): boolean {
  return forcedOnboarding || !explicitDemo;
}

export function canOpenSealedDemo(mode: RuntimeAvailabilityMode): boolean {
  return mode !== "checking";
}
