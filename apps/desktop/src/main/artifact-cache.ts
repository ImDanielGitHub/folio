/** Private local copy for the OS viewer. HTML is revealed, never automatically executed. */
import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { isValidArtifactId, type DownloadedArtifact } from "../artifact.js";

export async function cacheArtifact(
  temporaryRoot: string,
  artifactId: string,
  artifact: DownloadedArtifact,
): Promise<string> {
  if (!isValidArtifactId(artifactId) || !["pdf", "html"].includes(artifact.extension)) {
    throw new Error("Invalid cached artefact");
  }
  const directory = await mkdtemp(join(temporaryRoot, "folio-artefact-"));
  try {
    await chmod(directory, 0o700);
    const path = join(directory, `${artifactId}.${artifact.extension}`);
    await writeFile(path, artifact.bytes, { flag: "wx", mode: 0o600 });
    return path;
  } catch (error) {
    await rm(directory, { recursive: true, force: true });
    throw error;
  }
}
