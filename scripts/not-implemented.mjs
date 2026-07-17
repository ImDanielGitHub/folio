const [command = "unknown", lane = "unassigned"] = process.argv.slice(2);

console.error(`not implemented by lane yet: ${command} (${lane})`);
process.exitCode = 1;
