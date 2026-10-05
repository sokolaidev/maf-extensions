terraform {
  required_providers {
    random = {
      source  = "hashicorp/random"
      version = "3.9.1"
    }
  }
}

# The provider schema requires length; validation must report its absence.
resource "random_string" "example" {
  special = false
}
