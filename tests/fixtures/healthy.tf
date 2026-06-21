terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.100"
    }
  }
}

resource "aws_s3_bucket" "example" {
  bucket = "example-bucket"
}
