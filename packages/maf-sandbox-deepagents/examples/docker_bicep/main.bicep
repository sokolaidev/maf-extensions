// A storage account with two faults in it, on purpose.
//
// Both are things the Bicep toolchain catches and a model reading the file
// might not — which is the point of the sample. `build` and `lint` each
// report three: these two, plus `use-recent-api-versions`, on the age of the
// `apiVersion` below.
//
// Fixing the two (add a `sku`, delete or use the parameter) leaves that third.

@description('Where the storage account goes.')
param location string = resourceGroup().location

@description('Globally unique storage account name.')
@minLength(3)
@maxLength(24)
param storageAccountName string

// FAULT 2 — nothing references this parameter.
// `bicep lint` reports it as `no-unused-params` (an error — the repo config promotes it).
@description('Not used anywhere below. The linter should say so.')
param environmentName string

// FAULT 1 — `sku` is required on a storage account and is missing.
// `bicep build` reports `BCP035` (a warning): the declaration is missing the
// required property "sku".
//
// The API version below is deliberately a real one. An unrecognised
// `apiVersion` makes Bicep fall back to an untyped resource and stop checking
// properties altogether, which would silently erase the error above — the
// compiler would go quiet exactly where this file is trying to make noise.
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
  }
}

output storageAccountId string = storageAccount.id
