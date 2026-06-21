terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

resource "aws_ami" "example" {
  filter {
    name   = "name"
    values = ["example-*"]
  }
}

resource "aws_ecs_task_definition" "example" {
  family = "example"
}

resource "aws_s3_bucket" "example" {
  bucket = "example-bucket"
}
