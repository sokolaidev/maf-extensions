@description('A storage account with its location hardcoded — the fault this sample validates.')
param name string

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: name
  location: 'eastus'
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
}