// Frida agent for the apsd probe: ask Apple's own signer inside the loaded
// ApplePushService framework to sign the activation-info we built, and hand back
// the signature and certificate chain. Talks to Objective-C, so it has to be
// bundled with frida-objc-bridge rather than loaded as a bare script:
//
//     npx frida-compile apsd-agent.js -o apsd-agent.js -c
//
// Loaded by .github/scripts/probe_apsd.py into its --serve host.

import ObjC from "frida-objc-bridge";

const { NSData } = ObjC.classes;

const DER_SHA1_DIGEST_INFO = [
    0x30, 0x21, 0x30, 0x09, 0x06, 0x05, 0x2b, 0x0e,
    0x03, 0x02, 0x1a, 0x05, 0x00, 0x04, 0x14,
];
const DIGEST_INFO_SIZE = 35;
const SIGNATURE_SIZE = 128;
const CHAIN_CAPACITY = 4096;
const SCAN_LIMIT = 4096;

rpc.exports = {
    sign(activationInfoBase64) {
        const signer = locateSigner();

        const input = decodeBase64(activationInfoBase64);
        const digestInfo = Memory.alloc(DIGEST_INFO_SIZE);
        digestInfo.writeByteArray(DER_SHA1_DIGEST_INFO);
        ccSha1(input.bytes, input.length, digestInfo.add(DER_SHA1_DIGEST_INFO.length));

        const signature = Memory.alloc(SIGNATURE_SIZE);
        const signatureLength = Memory.alloc(4).writeU32(SIGNATURE_SIZE);
        const chain = Memory.alloc(CHAIN_CAPACITY);
        const chainLength = Memory.alloc(4).writeU32(CHAIN_CAPACITY);

        const status = signer(
            digestInfo, DIGEST_INFO_SIZE, signature, signatureLength, chain, chainLength,
        );
        const producedSignature = signatureLength.readU32();
        const producedChain = chainLength.readU32();
        if (status !== 0
                || producedSignature !== SIGNATURE_SIZE
                || producedChain < 1
                || producedChain > CHAIN_CAPACITY) {
            throw new Error("apsd signer rejected the activation-info "
                + `(status=${status}, signature=${producedSignature}, chain=${producedChain})`);
        }

        return {
            signature: encodeBase64(signature, producedSignature),
            certificate_chain: encodeBase64(chain, producedChain),
        };
    },
};

// apsd keeps its signer private, so anchor on the Objective-C method that calls
// it and take the first `bl` after the `mov w1, 0x23` that passes our 35-byte
// DigestInfo length.
function locateSigner() {
    const method = ObjC.classes.APSCertificateProvisioner["- generateClientIdentity"];
    if (method === undefined) {
        throw new Error("apsd has no -[APSCertificateProvisioner generateClientIdentity]");
    }
    const apsd = Process.getModuleByName("apsd");

    let cursor = stripPointerAuthentication(method.implementation);
    let sawDigestLength = false;
    for (let count = 0; count !== SCAN_LIMIT; count++) {
        const instruction = Instruction.parse(cursor);
        cursor = instruction.next;
        if (instruction.mnemonic === "bl" && sawDigestLength) {
            const target = ptr(instruction.operands[0].value.toString());
            if (isInside(target, apsd)) {
                return new NativeFunction(target, "int",
                    ["pointer", "uint32", "pointer", "pointer", "pointer", "pointer"]);
            }
        }
        sawDigestLength = movesImmediate(instruction, "w1", DIGEST_INFO_SIZE);
    }
    throw new Error("apsd signer not found: the scan no longer matches, re-derive it");
}

function movesImmediate(instruction, register, value) {
    const { mnemonic, operands } = instruction;
    if (mnemonic !== "mov" && mnemonic !== "movz") {
        return false;
    }
    if (operands.length !== 2) {
        return false;
    }
    const [destination, source] = operands;
    return destination.type === "reg" && destination.value === register
        && source.type === "imm" && source.value.toNumber() === value;
}

function isInside(address, module) {
    return address.compare(module.base) >= 0 && address.compare(module.base.add(module.size)) < 0;
}

// apsd is arm64e, so its IMPs carry pointer-authentication bits.
function stripPointerAuthentication(pointer) {
    return typeof pointer.strip === "function" ? pointer.strip("ia") : pointer;
}

function decodeBase64(text) {
    const data = NSData.alloc().initWithBase64EncodedString_options_(text, 0);
    return { bytes: data.bytes(), length: data.length().valueOf() };
}

function encodeBase64(bytes, length) {
    return NSData.dataWithBytes_length_(bytes, length).base64EncodedStringWithOptions_(0).toString();
}

const ccSha1 = new NativeFunction(
    Module.getGlobalExportByName("CC_SHA1"),
    "pointer",
    ["pointer", "uint32", "pointer"],
);
