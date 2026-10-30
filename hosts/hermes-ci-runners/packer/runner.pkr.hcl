packer {
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = ">= 1.3.0, < 2.0.0"
    }
  }
}

variable "region" {
  type    = string
  default = "us-west-2"
}
variable "runner_version" {
  type    = string
  default = "2.337.0"
}
variable "subnet_id" {
  type        = string
  default     = ""
  description = "Subnet for the build instance (the probe VPC public subnet, or any default-VPC subnet)."
}

source "amazon-ebs" "runner" {
  region                      = var.region
  instance_type               = "c7a.2xlarge"
  ssh_username                = "ubuntu"
  subnet_id                   = var.subnet_id == "" ? null : var.subnet_id
  associate_public_ip_address = true
  ami_name                    = "hermes-ci-runner-ubuntu2404-{{timestamp}}"
  ami_description             = "tfgar probe runner image (Ubuntu 24.04, gh-runner contents + docker + IMDS hook)"
  imds_support                = "v2.0"
  source_ami_filter {
    filters = {
      name                = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    owners      = ["099720109477"] # Canonical
    most_recent = true
  }
  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_size           = 80
    volume_type           = "gp3"
    delete_on_termination = true
  }
  tags = {
    Name    = "hermes-ci-runner"
    Project = "tfgar-probe"
  }
}

build {
  sources = ["source.amazon-ebs.runner"]

  # The file provisioner copies the contents of "files/" into an existing directory; create it first.
  provisioner "shell" {
    inline = ["mkdir -p /tmp/ghr-files"]
  }

  provisioner "file" {
    source      = "files/"
    destination = "/tmp/ghr-files"
  }
  provisioner "shell" {
    script           = "provision.sh"
    execute_command  = "sudo env {{ .Vars }} bash '{{ .Path }}'"
    environment_vars = ["RUNNER_VERSION=${var.runner_version}"]
  }
}
