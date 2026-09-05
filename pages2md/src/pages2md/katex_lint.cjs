// One read-only batch. No TeX compiler, shell commands, or network requests.
const fs = require("node:fs");
const katex = require(process.env.PAGES2MD_KATEX_MODULE || "katex");
const expressions = JSON.parse(fs.readFileSync(0, "utf8"));
const results = expressions.map(({tex, display}) => {
    const findings = [];
    try {
        katex.renderToString(tex, {
            displayMode: display,
            throwOnError: true,
            strict: false, // Syntax validation, not typography/style enforcement.
            maxExpand: 1000,
            maxSize: 100,
            macros: {}, // No macro state can leak between extracted formulas.
            trust: (context) => {
                findings.push({category: "unsupported", position: 0,
                    message: `Untrusted command disabled: ${context.command}`});
                return false;
            },
        });
    } catch (error) {
        if (!(error instanceof katex.ParseError)) throw error;
        const message = error.rawMessage || error.message;
        const category = /Undefined control sequence|No such environment|Unknown environment|not supported/i.test(message)
            ? "unsupported" : /Too many expansions/i.test(message) ? "resource_limit" : "syntax";
        findings.push({category, position: error.position || 0, message});
    }
    return findings;
});
process.stdout.write(JSON.stringify({version: katex.version, results}));
