// A storage account built from a public AVM module, and nothing else.
//
// The module reference is the whole reason this file is here. `br/public:` means
// the compiler cannot type-check anything until it has *downloaded* the module
// from the public Bicep registry, so this file compiles only in a sandbox that
// is allowed to reach it — which is what act 4 measures.
//
// Sample 05's `main.bicep` is the deliberate opposite: no modules, so it restores
// nothing and validates fully offline. The pair is the point. That file needs a
// closed sandbox to be honest; this one needs an allowlisted sandbox to be
// possible, and the four hosts it needs are declared by `bicep_sandbox_spec()`
// rather than by anything in this directory.
//
// The version below is pinned rather than floated. A moving reference would make
// a run's result depend on when it happened, and the live check would be reading
// the registry's release schedule instead of the sandbox's egress.

targetScope = 'resourceGroup'

@description('Globally unique storage account name.')
@minLength(3)
@maxLength(24)
param storageAccountName string

module storage 'br/public:avm/res/storage/storage-account:0.9.1' = {
  name: 'storageDeployment'
  params: {
    name: storageAccountName
    skuName: 'Standard_LRS'
    kind: 'StorageV2'
  }
}

output resourceId string = storage.outputs.resourceId
