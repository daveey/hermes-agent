# hermes-ci-runners (runner image)

Source of the AMI behind daveey/hermes's ephemeral runners. The runners themselves are the Spacelift stack
`devops/tf/hermes-ci-runners` in Metta-AI/metta (infrastructure account 751442549699, us-east-1); the stack that lived
here in 767406518141 was destroyed on 2026-10-30.

    packer/      Ubuntu 24.04 AMI: gh-runner contents + libatomic1, Tauri/X11 headers, docker-ce, buildx, awscli, runner agent
      files/ghr-imds-hook   fail-closed IMDS self-disable (runs before the runner starts)
      files/ghr-start       boot entrypoint: hook fails => poweroff, runner never starts
    bootstrap/   GitHub App manifest + credential storage (SSM)
    probe/       make-probe-branch.sh: branch whose 5 target workflows run only on tfgar-probe
    tests/       test_imds_hook.sh (stubbed curl/aws; no AWS needed)

## Rolling out an image
1. `packer init packer/ && packer build -var subnet_id=<default-VPC subnet> packer/` in 767406518141 us-west-2 -> AMI id.
2. Share the AMI and its snapshot with 751442549699.
3. Bump `source_ami` in the metta stack. Its `aws_ami_copy` copies the image to us-east-1, encrypted.
4. Run the probe (`tfgar-probe.yml`) on the `tfgar-probe` label before relying on it.

## IMDS design
The instance role needs IMDS for its own credentials, so the hook reads identity, tags and the JIT config first,
deletes the one-shot SSM parameter, calls `ec2:ModifyInstanceMetadataOptions --http-endpoint disabled` on itself, then
requires BOTH `describe-instances` to say `disabled` and `curl 169.254.169.254` to fail. Credentials only live in
non-exported shell variables of the hook process, which exits before the runner starts. Any failure => `poweroff`
(launch template `instance_initiated_shutdown_behavior = terminate`). Extra IAM: ModifyInstanceMetadataOptions on self
(`aws:ARN = ${ec2:SourceInstanceARN}`) and DescribeInstances.

## Known deviations / open points
- Cloudwatch agent is off: it needs IMDS credentials after the hook disables them. Debug via the SSM-less path: none; use EC2 console output.
- pm-bundle on daveey/hermes is skipped (`if: github.repository != 'daveey/hermes'`) and has no linux leg; the probe branch adds one native linux-x64 leg.
- remote-backends.yml is `workflow_call` only; the probe branch adds `workflow_dispatch`.
- Public subnets with public IPv4 (no NAT cost); NACL blocks private ranges except the VPC's own CIDR (DNS resolver).
